"""DesktopTool deterministic behavior against FakeDesktopBackend (SPEC §10
fake-adapter rule): dispatch, bounds, honest failures, bounded evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.desktop.backend import (
    FakeDesktopBackend,
    UnavailableDesktopBackend,
    default_backend,
    is_win32_backend,
)
from nomadicos.desktop.contracts import MAX_CAPTURE_DIM, MAX_TITLE_CHARS, MAX_WINDOWS
from nomadicos.kernel.errors import Failure, InvalidProposal
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.desktop import DesktopTool

OPS = {
    "screenshot",
    "mouse_move",
    "mouse_click",
    "mouse_scroll",
    "type_text",
    "press_key",
    "hotkey",
    "list_windows",
    "foreground_window",
    "focus_window",
    "ui_tree",
    "ui_find",
    "ui_invoke",
    "ui_set_value",
    "ui_focus",
}


def ctx(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext.for_task("task_desk", tmp_path / "ws")


def make(width: int = 1920, height: int = 1080) -> tuple[DesktopTool, FakeDesktopBackend]:
    backend = FakeDesktopBackend(width=width, height=height)
    return DesktopTool(backend), backend


async def observe(tool: DesktopTool, tmp_path: Path) -> None:
    """Raw input needs a prior look at the foreground window (stale-target guard)."""
    seen = await tool.run("foreground_window", {}, ctx(tmp_path))
    assert seen.status is ExecutionStatus.SUCCEEDED


# ------------------------------------------------------------- contract ----


def test_tool_contract_and_ops() -> None:
    tool = DesktopTool(FakeDesktopBackend())
    assert tool.name == "desktop"
    assert set(tool.ops) == OPS


def test_registry_catalog_exposes_typed_desktop_operations() -> None:
    registry = ToolRegistry()
    registry.register(DesktopTool(FakeDesktopBackend()))
    desktop_lines = [ln for ln in registry.catalog_lines() if "- desktop:" in ln]
    assert len(desktop_lines) == len(OPS)
    assert '- desktop: operation "screenshot" args: {}' in desktop_lines
    assert '- desktop: operation "mouse_move" args: {x,y}' in desktop_lines


def test_capability_resolution_and_args_validation() -> None:
    registry = ToolRegistry()
    registry.register(DesktopTool(FakeDesktopBackend()))
    assert registry.capability("desktop", "mouse_move").capability == "desktop.mouse_move"
    assert registry.has("desktop", "focus_window")
    with pytest.raises(InvalidProposal):
        registry.check_args("desktop", "mouse_move", {"x": -1, "y": 0})
    with pytest.raises(InvalidProposal):
        registry.check_args("desktop", "format_disk", {})


# -------------------------------------------------------------- mouse ------


async def test_mouse_move_within_bounds_records(tmp_path) -> None:
    tool, backend = make()
    outcome = await tool.run("mouse_move", {"x": 10, "y": 20}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.cursor == (10, 20)
    assert outcome.evidence == {"x": 10, "y": 20, "screen": [1920, 1080]}


async def test_mouse_move_outside_screen_rejected_never_clamped(tmp_path) -> None:
    tool, backend = make(width=100, height=80)
    outcome = await tool.run("mouse_move", {"x": 999, "y": 5}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert "outside screen" in outcome.message
    assert backend.cursor == (0, 0)
    assert outcome.evidence["requested"] == [999, 5]


async def test_mouse_click_records_button_and_clicks(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    outcome = await tool.run(
        "mouse_click", {"x": 30, "y": 40, "button": "right", "clicks": 2}, ctx(tmp_path)
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.clicks == [(30, 40, "right", 2)]
    assert outcome.evidence["button"] == "right"


async def test_mouse_scroll_bounded(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    outcome = await tool.run("mouse_scroll", {"amount": -3}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.scrolled == [-3]
    bad = await tool.run("mouse_scroll", {"amount": 11}, ctx(tmp_path))
    assert bad.status is ExecutionStatus.ERRORED


# ----------------------------------------------------------- keyboard ------


async def test_type_text_records_and_bounds(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    outcome = await tool.run("type_text", {"text": "hello"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.typed == ["hello"]
    assert outcome.evidence["typed_chars"] == 5
    bad = await tool.run("type_text", {"text": ""}, ctx(tmp_path))
    assert bad.status is ExecutionStatus.ERRORED


async def test_press_key_normalizes_and_records(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    outcome = await tool.run("press_key", {"key": "CTRL+ALT+Delete"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["keys"] == ["ctrl", "alt", "delete"]
    assert backend.keys == ["ctrl+alt+delete"]


# ------------------------------------------------------------- windows -----


async def test_list_windows_returns_titles(tmp_path) -> None:
    tool, _ = make()
    outcome = await tool.run("list_windows", {"limit": 1}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["count"] == 1
    assert outcome.evidence["titles"] == ["Untitled - Notepad"]


async def test_window_titles_truncated_and_capped(tmp_path) -> None:
    tool, backend = make()
    backend.windows = [{"handle": i, "title": f"{'x' * 300} #{i}"} for i in range(MAX_WINDOWS + 5)]
    outcome = await tool.run("list_windows", {}, ctx(tmp_path))
    # listing is bounded at MAX_WINDOWS: the tool caps what it reports
    assert outcome.evidence["count"] == MAX_WINDOWS
    titles = outcome.evidence["titles"]
    assert len(titles) == MAX_WINDOWS
    assert all(len(t) <= MAX_TITLE_CHARS for t in titles)


async def test_foreground_window_returns_bounded_data(tmp_path) -> None:
    tool, _ = make()
    outcome = await tool.run("foreground_window", {}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["title"] == "Untitled - Notepad"


async def test_focus_window_hit_and_miss(tmp_path) -> None:
    tool, backend = make()
    hit = await tool.run("focus_window", {"title": "notepad"}, ctx(tmp_path))
    assert hit.status is ExecutionStatus.SUCCEEDED
    assert hit.evidence["focused"] is True
    assert backend.focused == "Untitled - Notepad"
    miss = await tool.run("focus_window", {"title": "no-such-window-xyz"}, ctx(tmp_path))
    assert miss.status is ExecutionStatus.FAILED
    assert miss.failure is Failure.ACTION_FAILED
    assert "not found" in miss.message


async def test_focus_window_handle_pins_the_observed_window(tmp_path) -> None:
    tool, backend = make()
    backend.windows = [
        {"handle": 1, "title": "Budget - draft"},
        {"handle": 2, "title": "Budget - final"},
    ]
    hit = await tool.run("focus_window", {"title": "Budget - final", "handle": 2}, ctx(tmp_path))
    assert hit.status is ExecutionStatus.SUCCEEDED
    assert hit.evidence["handle"] == 2
    assert backend.focused == "Budget - final"

    stale = await tool.run("focus_window", {"title": "Budget - final", "handle": 1}, ctx(tmp_path))
    assert stale.status is ExecutionStatus.FAILED
    assert stale.evidence["handle"] == 1
    assert "no longer matches" in stale.message
    assert backend.focused == "Budget - final"


# ---------------------------------------------------------- screenshot -----


async def test_screenshot_writes_bounded_png_artifact(tmp_path) -> None:
    tool, _ = make(width=64, height=32)
    outcome = await tool.run("screenshot", {}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    evidence = outcome.evidence
    assert evidence["width"] == 64 and evidence["height"] == 32
    artifact = Path(evidence["path"])
    data = artifact.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert evidence["bytes"] == len(data)
    assert len(evidence["sha256"]) == 64
    assert artifact.parent.name == "desktop"


async def test_screenshot_rejects_oversized_screen(tmp_path) -> None:
    tool, _ = make(width=MAX_CAPTURE_DIM + 1, height=10)
    outcome = await tool.run("screenshot", {}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert "capture bound" in outcome.message


async def test_screenshot_zero_screen_is_honest_error(tmp_path) -> None:
    tool, _ = make(width=0, height=0)
    outcome = await tool.run("screenshot", {}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.ERRORED
    assert outcome.failure is Failure.RESOURCE_UNAVAILABLE


# ---------------------------------------------------------- unavailability -


async def test_unavailable_backend_never_fakes_success(tmp_path) -> None:
    tool = DesktopTool(UnavailableDesktopBackend())
    for operation, args in (
        ("screenshot", {}),
        ("mouse_move", {"x": 1, "y": 2}),
        ("mouse_click", {"x": 1, "y": 2}),
        ("mouse_scroll", {"amount": 1}),
        ("type_text", {"text": "x"}),
        ("press_key", {"key": "a"}),
        ("list_windows", {}),
        ("foreground_window", {}),
        ("focus_window", {"title": "x"}),
    ):
        outcome = await tool.run(operation, args, ctx(tmp_path))
        assert outcome.status is ExecutionStatus.FAILED, operation
        assert outcome.failure is Failure.RESOURCE_UNAVAILABLE, operation
        assert "desktop unavailable" in outcome.message


def test_default_backend_is_honest_off_windows() -> None:
    import sys

    backend = default_backend()
    if sys.platform == "win32":
        assert is_win32_backend(backend)
    else:
        assert isinstance(backend, UnavailableDesktopBackend)


# --------------------------------------------------------------- guards ----


async def test_unsupported_operation_fails_honestly(tmp_path) -> None:
    tool, _ = make()
    outcome = await tool.run("definitely-not-an-op", {}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.ERRORED
    assert outcome.failure is Failure.TOOL_ERROR
    assert "no operation" in outcome.message


async def test_outcomes_are_deterministic(tmp_path) -> None:
    tool, _ = make()
    first = await tool.run("list_windows", {"limit": 2}, ctx(tmp_path))
    second = await tool.run("list_windows", {"limit": 2}, ctx(tmp_path))
    assert first.status is second.status
    assert first.evidence == second.evidence
    assert first.duration_s >= 0.0 and second.duration_s >= 0.0


# --------------------------------------------- Phase 12B failure semantics -


# --------------------------------------------- Phase 12B failure semantics -


async def test_failures_leave_authority_state_unchanged(tmp_path) -> None:
    """Structured failures only: no fake evidence, no authority mutation,
    no automatic retry inside the tool."""
    from nomadicos.authority.store import AuthorityStore

    store = AuthorityStore(tmp_path / "state" / "authority.json")
    store.grant_full_pc_autonomy()
    before = store.state()
    backend = FakeDesktopBackend(width=100, height=80)
    tool = DesktopTool(backend)
    ctx_ = ExecutionContext.for_task("task_fail", tmp_path / "ws")
    outcomes = [
        await tool.run("mouse_move", {"x": 5000, "y": 1}, ctx_),  # out of bounds
        await tool.run("focus_window", {"title": "ghost-window-xyz"}, ctx_),  # missing window
        await tool.run("press_key", {"key": "not-a-key+++"}, ctx_),  # malformed key
        await tool.run("nope", {}, ctx_),  # unsupported operation
    ]
    for outcome in outcomes:
        assert outcome.status in (ExecutionStatus.FAILED, ExecutionStatus.ERRORED)
        assert outcome.status is not ExecutionStatus.SUCCEEDED
    assert backend.cursor == (0, 0) and backend.clicks == [] and backend.keys == []
    # structured, honest: each failure carries a message, none fabricate success
    assert all(o.message for o in outcomes)
    after = store.state()
    assert after.epoch == before.epoch
    assert after.grant == before.grant


# ------------------------------------------------ stale-target guard ------


async def test_input_without_observation_is_refused(tmp_path) -> None:
    tool, backend = make()
    for op, args in (
        ("type_text", {"text": "secret"}),
        ("press_key", {"key": "enter"}),
        ("hotkey", {"keys": ["alt", "f4"]}),
        ("mouse_click", {"x": 1, "y": 1}),
        ("mouse_scroll", {"amount": 1}),
    ):
        outcome = await tool.run(op, args, ctx(tmp_path))
        assert outcome.status is ExecutionStatus.FAILED, op
        assert outcome.failure is Failure.ACTION_FAILED
        assert outcome.evidence["stale_target"] is True
        assert "observe first" in outcome.message
    assert backend.typed == backend.keys == backend.hotkeys == backend.scrolled == []
    assert backend.clicks == []


async def test_focus_change_after_observation_refuses_input(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    backend.windows.insert(0, {"handle": 9, "title": "Pop-up", "pid": 909})
    outcome = await tool.run("type_text", {"text": "hello"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert "changed since it was observed" in outcome.message
    assert outcome.evidence["foreground_title"] == "Pop-up"
    assert backend.typed == []


async def test_same_handle_new_process_instance_is_refused(tmp_path) -> None:
    tool, backend = make()
    backend.windows[0]["started"] = 1
    await observe(tool, tmp_path)
    backend.windows[0]["started"] = 2  # same handle and pid, restarted process
    outcome = await tool.run("press_key", {"key": "enter"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert backend.keys == []


async def test_observation_does_not_carry_across_tasks(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)  # task_desk
    other = ExecutionContext.for_task("task_other", tmp_path / "ws")
    outcome = await tool.run("type_text", {"text": "x"}, other)
    assert outcome.status is ExecutionStatus.FAILED
    assert "no observation" in outcome.message
    assert backend.typed == []


async def test_focus_window_counts_as_observation(tmp_path) -> None:
    tool, backend = make()
    focused = await tool.run("focus_window", {"title": "NomadicOS Console"}, ctx(tmp_path))
    assert focused.status is ExecutionStatus.SUCCEEDED
    outcome = await tool.run("type_text", {"text": "dir"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.typed == ["dir"]


async def test_ui_read_without_foreground_handle_is_not_an_observation(tmp_path) -> None:
    from nomadicos.desktop.uia import FakeUIA

    backend = FakeDesktopBackend()
    uia = FakeUIA({"Other": [{"control_type": "button", "name": "OK", "depth": 1}]})
    tool = DesktopTool(backend, uia=uia)
    read = await tool.run("ui_find", {"window": "Other", "name": "OK"}, ctx(tmp_path))
    assert read.status is ExecutionStatus.SUCCEEDED
    outcome = await tool.run("type_text", {"text": "x"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert backend.typed == []


async def test_click_over_another_application_is_refused(tmp_path) -> None:
    tool, backend = make()
    await observe(tool, tmp_path)
    backend.window_at = lambda x, y: {"handle": 7, "pid": 707}  # type: ignore[method-assign]
    outcome = await tool.run("mouse_click", {"x": 5, "y": 5}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.FAILED
    assert "another application" in outcome.message
    assert backend.clicks == []
    # a menu or dialog of the SAME process is a different window but allowed
    backend.window_at = lambda x, y: {"handle": 8, "pid": 101}  # type: ignore[method-assign]
    ok = await tool.run("mouse_click", {"x": 5, "y": 5}, ctx(tmp_path))
    assert ok.status is ExecutionStatus.SUCCEEDED
    assert backend.clicks == [(5, 5, "left", 1)]


async def test_named_target_window_admits_input_without_observation(tmp_path) -> None:
    tool, backend = make()
    ok = await tool.run("type_text", {"text": "hi", "window": "notepad"}, ctx(tmp_path))
    assert ok.status is ExecutionStatus.SUCCEEDED
    assert backend.typed == ["hi"]
    wrong = await tool.run("press_key", {"key": "enter", "window": "Finance"}, ctx(tmp_path))
    assert wrong.status is ExecutionStatus.FAILED
    assert 'not "Finance"' in wrong.message
    assert backend.keys == []
