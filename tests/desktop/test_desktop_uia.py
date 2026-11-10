"""UI Automation through the desktop tool (SPEC §26, Phase 12).

Controls are read and acted on by name / automation id / control type, never
by pixel. The fake adapter makes every test deterministic; one real-Notepad
test (``hardware``) exercises the comtypes adapter on the owner's desktop.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from desktop_helpers import desktop_json, make_desktop_app
from pydantic import ValidationError

from nomadicos.contracts.core import parse_predicate
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.desktop.contracts import UiFindArgs, UiSetValueArgs, UiTreeArgs
from nomadicos.desktop.uia import FakeUIA, UiActionFailed
from nomadicos.tools import DesktopTool, ExecutionContext
from nomadicos.tools.desktop import MAX_UI_RENDER_CHARS, render_ui
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import evaluate_goal_predicate

NOTEPAD = "notes.txt - Notepad"


def notepad_controls() -> list[dict]:
    return [
        {"control_type": "window", "name": NOTEPAD, "depth": 0, "actions": ["focus"]},
        {
            "control_type": "document",
            "name": "Text editor",
            "depth": 2,
            "value": "draft",
            "actions": ["focus", "set_value"],
        },
        {
            "control_type": "menuitem",
            "name": "File",
            "automation_id": "File",
            "depth": 4,
            "actions": ["focus", "invoke"],
        },
        {
            "control_type": "button",
            "name": "Save",
            "depth": 4,
            "enabled": False,
            "actions": ["invoke"],
        },
        {
            "control_type": "edit",
            "name": "Password",
            "depth": 4,
            "password": True,
            "actions": ["set_value"],
        },
    ]


def tool(uia: FakeUIA | None = None) -> DesktopTool:
    return DesktopTool(FakeDesktopBackend(), uia=uia or FakeUIA({NOTEPAD: notepad_controls()}))


def ctx(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext.for_task("task_ui", tmp_path)


# ---------------------------------------------------------- contracts ----


def test_queries_need_a_criterion_and_a_known_control_type() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        UiFindArgs(window="Notepad")
    with pytest.raises(ValidationError, match="unknown control type"):
        UiFindArgs(control_type="widget")
    assert UiFindArgs(control_type="Button").control_type == "button"
    with pytest.raises(ValidationError):
        UiTreeArgs(max_nodes=10_000)
    with pytest.raises(ValidationError):
        UiSetValueArgs(name="x", value="v", surprise=True)


# --------------------------------------------------------------- tool ----


async def test_tree_is_rendered_for_the_model_and_kept_as_evidence(tmp_path: Path) -> None:
    out = await tool().run("ui_tree", {"window": "notepad"}, ctx(tmp_path))
    assert out.status is ExecutionStatus.SUCCEEDED
    assert out.evidence["title"] == NOTEPAD and out.evidence["count"] == 5
    assert 'document "Text editor" [focus,set_value] value="draft"' in out.stdout
    assert 'button "Save" [invoke] disabled' in out.stdout
    # a password field is listed, its value never is
    assert "value=<password>" in out.stdout


async def test_find_matches_names_case_insensitively_and_fails_honestly(tmp_path: Path) -> None:
    t = tool()
    found = await t.run("ui_find", {"name": "file"}, ctx(tmp_path))
    assert found.status is ExecutionStatus.SUCCEEDED
    assert [e["automation_id"] for e in found.evidence["elements"]] == ["File"]
    missing = await t.run("ui_find", {"name": "Print"}, ctx(tmp_path))
    assert missing.status is ExecutionStatus.FAILED
    assert "no control matches" in missing.message


async def test_set_value_reports_the_re_read_control(tmp_path: Path) -> None:
    uia = FakeUIA({NOTEPAD: notepad_controls()})
    out = await tool(uia).run(
        "ui_set_value", {"control_type": "document", "value": "hello"}, ctx(tmp_path)
    )
    assert out.status is ExecutionStatus.SUCCEEDED
    assert out.evidence["element"]["value"] == "hello"
    assert uia.log == [("set_value", NOTEPAD, "hello")]


async def test_disabled_missing_or_unsupported_controls_are_failed_steps(
    tmp_path: Path,
) -> None:
    t = tool()
    for op, args in (
        ("ui_invoke", {"name": "Save"}),  # disabled
        ("ui_invoke", {"name": "Print"}),  # missing
        ("ui_set_value", {"name": "File", "value": "x"}),  # no writable value
        ("ui_tree", {"window": "Calculator"}),  # no such window
    ):
        out = await t.run(op, args, ctx(tmp_path))
        assert out.status is ExecutionStatus.FAILED, (op, args)
        assert out.failure is not None


def test_render_is_bounded() -> None:
    many = [{"control_type": "listitem", "name": f"item {i}", "depth": 1} for i in range(500)]
    text = render_ui({"title": "Big"}, many, truncated=True)
    assert len(text) <= MAX_UI_RENDER_CHARS
    assert text.endswith("(cut short)")


# --------------------------------------------------------- predicate ----


def ui_exec(evidence: dict, *, status: ExecutionStatus = ExecutionStatus.SUCCEEDED):
    return ExecutionResult(
        task_id="task_ui",
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="desktop",
        operation="ui_find",
        status=status,
        evidence=evidence,
    )


def verdict(expr: dict, executions: list[ExecutionResult], tmp_path: Path):
    ctx = EvidenceContext(task_id="task_ui", workspace=tmp_path, executions=executions)
    return evaluate_goal_predicate(parse_predicate(expr), ctx).verdict


def test_ui_element_present_reads_only_observed_controls(tmp_path: Path) -> None:
    seen = ui_exec({"title": NOTEPAD, "elements": notepad_controls()})
    later = ui_exec(
        {
            "title": NOTEPAD,
            "element": {"control_type": "document", "name": "Text editor", "value": "hello"},
        }
    )
    present = {"type": "ui_element_present", "window": "notepad", "name": "save"}
    assert verdict(present, [seen], tmp_path) is VerificationOutcome.PASS
    absent = {"type": "ui_element_present", "name": "Print"}
    assert verdict(absent, [seen], tmp_path) is VerificationOutcome.NOT_PASS
    # the most recent observation of a control decides its value
    value = {"type": "ui_element_present", "control_type": "document", "value": "hello"}
    assert verdict(value, [seen, later], tmp_path) is VerificationOutcome.PASS
    assert verdict(value, [later, seen], tmp_path) is VerificationOutcome.NOT_PASS
    contains = {"type": "ui_element_present", "control_type": "document", "value_contains": "ell"}
    assert verdict(contains, [seen, later], tmp_path) is VerificationOutcome.PASS


def test_ui_element_present_without_ui_evidence_is_not_verified(tmp_path: Path) -> None:
    failed = ui_exec(
        {"title": NOTEPAD, "elements": notepad_controls()}, status=ExecutionStatus.FAILED
    )
    expr = {"type": "ui_element_present", "name": "Save"}
    assert verdict(expr, [], tmp_path) is VerificationOutcome.NOT_VERIFIED
    assert verdict(expr, [failed], tmp_path) is VerificationOutcome.NOT_VERIFIED
    # a password field never yields a value to compare against
    pw = {"type": "ui_element_present", "name": "Password", "value": "x"}
    seen = ui_exec({"title": NOTEPAD, "elements": notepad_controls()})
    assert verdict(pw, [seen], tmp_path) is VerificationOutcome.NOT_PASS


# ------------------------------------------------------- full graph -----


async def test_graph_sets_text_by_control_and_proves_it(tmp_path: Path) -> None:
    uia = FakeUIA({NOTEPAD: notepad_controls()})
    app = make_desktop_app(
        tmp_path,
        scripts=[
            (
                "Notepad",
                desktop_json(
                    "ui_set_value",
                    {"window": "Notepad", "control_type": "document", "value": "hello"},
                ),
            )
        ],
    )
    app.tools.register(DesktopTool(FakeDesktopBackend(width=800, height=600), uia=uia))
    summary = await app.run_goal(
        "Write hello into the Notepad document",
        predicates=[
            {
                "type": "ui_element_present",
                "window": "Notepad",
                "control_type": "document",
                "value": "hello",
            }
        ],
    )
    assert summary.status.value == "SUCCESS", summary.outcome_note
    assert ("set_value", NOTEPAD, "hello") in uia.log


async def test_owner_rule_on_a_window_pauses_ui_actions(tmp_path: Path) -> None:
    uia = FakeUIA({NOTEPAD: notepad_controls()})
    app = make_desktop_app(
        tmp_path,
        scripts=[
            (
                "Notepad",
                desktop_json(
                    "ui_set_value", {"window": "Notepad", "control_type": "document", "value": "x"}
                ),
            )
        ],
    )
    app.tools.register(DesktopTool(FakeDesktopBackend(width=800, height=600), uia=uia))
    app.store.add_instruction("do not touch my notes", "Notepad")
    summary = await app.run_goal("Write x into the Notepad document")
    assert summary.waiting_owner()
    assert uia.log == []  # nothing ran while the owner is asked


# ------------------------------------------------------ real desktop ----


@pytest.mark.hardware
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_real_notepad_round_trip(tmp_path: Path) -> None:
    """Opens Notepad on a throwaway file, sets its text through UIA, reads it
    back, then closes that Notepad without saving."""
    import psutil

    from nomadicos.desktop.uia import ComtypesUIA

    # Windows 11 Notepad hosts every window in one process: closing ours by
    # pid would close the owner's own Notepad windows and lose unsaved text
    if any(
        (p.info.get("name") or "").lower() == "notepad.exe" for p in psutil.process_iter(["name"])
    ):
        pytest.skip("Notepad is already open; this test would share its process")
    target = tmp_path / "nomadic-uia-test.txt"
    target.write_text("start", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(target)])  # noqa: S603, S607
    t = DesktopTool(FakeDesktopBackend(), uia=ComtypesUIA())
    tree = None
    for _ in range(40):
        tree = await t.run("ui_tree", {"window": "nomadic-uia-test", "max_depth": 3}, ctx(tmp_path))
        if tree.status is ExecutionStatus.SUCCEEDED:
            break
        time.sleep(0.25)
    assert tree is not None and tree.status is ExecutionStatus.SUCCEEDED, tree and tree.message
    try:
        out = await t.run(
            "ui_set_value",
            {"window": "nomadic-uia-test", "control_type": "document", "value": "from uia"},
            ctx(tmp_path),
        )
        assert out.status is ExecutionStatus.SUCCEEDED, out.message
        assert out.evidence["element"]["value"].strip() == "from uia"
    finally:
        pid = tree.evidence["window"]["process_id"]
        psutil.Process(pid).kill()
    with pytest.raises(UiActionFailed):
        FakeUIA({}).tree(None, 1, 1)
