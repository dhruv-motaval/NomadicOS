"""Focused regression coverage: desktop keyboard action contract.

The reported runtime failure: the model hallucinated the proposal
``press_key.press_key`` (unknown tool) and the run was BLOCKED with
INVALID_PROPOSAL. The fix adds a first-class canonical ``desktop.hotkey``
operation (typed structured chord args) through the FULL existing pipeline:
Action IR -> ToolCatalog -> validation -> authorization -> executor ->
DesktopTool -> DesktopBackend -> Win32. The model output remains untrusted;
authorization remains mandatory; the model can never invoke the backend
directly.
"""

from __future__ import annotations

import pytest
from desktop_helpers import desktop_json, make_desktop_app  # tests/desktop

from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.desktop.contracts import HotkeyArgs
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.tools import ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.desktop import DesktopTool

REGRESSION_GOAL = "can you press 'win + d' key on keyboard"


def ctx_for(tmp_path) -> ExecutionContext:
    return ExecutionContext.for_task("t1", tmp_path)


# ------------------------------------------------- contract resolution ----


def test_desktop_press_key_resolves() -> None:
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    assert tools.has("desktop", "press_key")
    assert tools.capability("desktop", "press_key") is not None


def test_desktop_hotkey_resolves() -> None:
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    assert tools.has("desktop", "hotkey")
    assert tools.capability("desktop", "hotkey") is not None
    # the catalog exposes the canonical first-class operation
    assert any('desktop: operation "hotkey"' in line for line in tools.catalog_lines())


def test_press_key_press_key_is_rejected() -> None:
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    with pytest.raises(InvalidProposal, match="unknown action press_key.press_key"):
        tools.capability("press_key", "press_key")


def test_press_key_press_key_action_accepted_only_if_registered() -> None:
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    # unless that EXACT action is intentionally registered it stays invalid;
    # when registered it resolves (explicit contract, not a guess)
    with pytest.raises(InvalidProposal):
        tools.capability("press_key", "press_key")

    from nomadicos.contracts.execution import ExecutionStatus
    from nomadicos.tools.base import Tool, ToolOutcome

    class _PressKeyTool(Tool):
        name = "press_key"
        ops = {"press_key": HotkeyArgs}

        async def run(self, operation: str, args: dict, ctx: ExecutionContext) -> ToolOutcome:
            return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence={"keys": ["test"]})

    tools.register(_PressKeyTool())
    assert tools.has("press_key", "press_key")
    assert tools.capability("press_key", "press_key") is not None


# ---------------------------------------------------- hotkey validation ----


def test_hotkey_typed_args_validate() -> None:
    typed = HotkeyArgs.model_validate({"keys": ["Win", "D"]})
    assert typed.keys == ["win", "d"]  # closed-vocabulary, lowercased


@pytest.mark.parametrize(
    "args",
    [
        {"keys": []},  # empty chord
        {"keys": ["win", "d", "shift", "alt", "ctrl"]},  # over the bound (5 > 4)
        {"keys": ["win", "notakey"]},  # unknown key
        {"keys": "win+d"},  # malformed: not a list
        {"keys": ["win", "d"], "extra": 1},  # unknown field fails closed
        {},  # missing keys
    ],
)
async def test_malformed_hotkey_args_rejected(args: dict, tmp_path) -> None:
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    outcome = await tools.get("desktop").run("hotkey", args, ctx_for(tmp_path))
    assert outcome.status.value == "ERRORED"
    assert outcome.failure is not None and outcome.failure.value == "TOOL_ERROR"


async def test_validation_error_is_human_readable(tmp_path) -> None:
    """The replanning layer must be able to self-correct from the error:
    readable text (loc + msg), not opaque raw pydantic dicts."""
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    outcome = await tools.get("desktop").run("hotkey", {"keys": "win+d"}, ctx_for(tmp_path))
    assert "keys: Input should be a valid list" in outcome.message
    assert "'type'" not in outcome.message and "'loc'" not in outcome.message


def test_ir_validation_error_is_human_readable() -> None:
    from nomadicos.action_ir.parser import parse_model_output
    from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
    from nomadicos.kernel.events import EventLogger

    proposal = parse_model_output(
        '{"tool": "desktop", "operation": "hotkey", "args": {"keys": "win+d"}}',
        task_id="t1",
        step_id="s1",
        model_id="models/small.gguf",
    )
    tools = ToolRegistry()
    tools.register(DesktopTool(FakeDesktopBackend()))
    with pytest.raises(InvalidProposal) as excinfo:
        ProposalValidator(tools, EventLogger()).validate(
            proposal.value,
            ValidationContext(task_id="t1", step_id="s1", model_id="models/small.gguf"),
        )
    assert "arguments invalid" in excinfo.value.message
    assert "keys" in excinfo.value.message
    assert "valid list" in excinfo.value.message
    assert "'loc'" not in excinfo.value.message


# ------------------------------------------------------- backend reach -----


async def test_win_d_reaches_the_desktop_backend(tmp_path) -> None:
    backend = FakeDesktopBackend()
    tools = ToolRegistry()
    tools.register(DesktopTool(backend))
    outcome = await tools.get("desktop").run("hotkey", {"keys": ["Win", "D"]}, ctx_for(tmp_path))
    assert outcome.status.value == "SUCCEEDED"
    # the typed structured chord reached the desktop backend
    assert backend.hotkeys == [["win", "d"]]
    assert outcome.evidence["keys"] == ["win", "d"]


async def test_press_key_semantics_preserved(tmp_path) -> None:
    backend = FakeDesktopBackend()
    tools = ToolRegistry()
    tools.register(DesktopTool(backend))
    outcome = await tools.get("desktop").run("press_key", {"key": "enter"}, ctx_for(tmp_path))
    assert outcome.status.value == "SUCCEEDED"
    assert backend.keys == ["enter"]


# ------------------------------------------------- full pipeline / auth ----


async def test_regression_goal_produces_canonical_hotkey(tmp_path) -> None:
    """The EXACT user request: the first proposal is the true hallucination
    (tool press_key, unknown action — rejected fail-closed); recovery
    exposes the validation error and the registered actions; the retry
    proposal becomes a VALID canonical desktop.hotkey action which is
    authorized and executed. The goal carries no completion predicate, so
    SUCCESS is never manufactured: the honest outcome stays PARTIAL."""
    from nomadicos.contracts.core import TaskStatus

    app = make_desktop_app(
        tmp_path,
        scripts=[
            (
                "win + d",
                [
                    '{"tool": "press_key", "operation": "press_key", "args": {"key": "win+d"}}',
                    desktop_json("hotkey", {"keys": ["win", "d"]}),
                    # the two-step investigate->act plan gives a second
                    # proposal turn; a compliant model submits a claim once
                    # the chord is typed (the mock replays its last reply
                    # otherwise, which would double-execute)
                    '{"finished": true}',
                ],
            )
        ],
    )
    backend = app.tools.get("desktop").backend
    summary = await app.run_goal(REGRESSION_GOAL)
    # the canonical desktop.hotkey action was authorized and executed; the
    # goal has no predicate so the honest status is PARTIAL (no SUCCESS
    # manufacture without GoalVerifier evidence)
    assert summary.status is TaskStatus.PARTIAL
    assert summary.goal_verdict != "PASS"
    # the model NEVER bypassed authorization: the action went through the
    # pipeline and the desktop backend received the typed chord
    assert backend.hotkeys == [["win", "d"]]
    assert backend.keys == []  # press_key was never executed


async def test_authorization_remains_mandatory(tmp_path) -> None:
    app = make_desktop_app(
        tmp_path,
        scripts=[("win + d", desktop_json("hotkey", {"keys": ["win", "d"]}))],
        grant=False,
    )
    backend = app.tools.get("desktop").backend
    summary = await app.run_goal(REGRESSION_GOAL)
    # no grant: no execution, honestly recorded — the backend is untouched
    assert backend.hotkeys == []
    assert summary.status.value in ("WAITING_OWNER", "BLOCKED", "FAILED", "PARTIAL")
