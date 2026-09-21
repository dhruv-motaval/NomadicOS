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
    "list_windows",
    "foreground_window",
    "focus_window",
}


def ctx(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext.for_task("task_desk", tmp_path / "ws")


def make(width: int = 1920, height: int = 1080) -> tuple[DesktopTool, FakeDesktopBackend]:
    backend = FakeDesktopBackend(width=width, height=height)
    return DesktopTool(backend), backend


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
    outcome = await tool.run(
        "mouse_click", {"x": 30, "y": 40, "button": "right", "clicks": 2}, ctx(tmp_path)
    )
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.clicks == [(30, 40, "right", 2)]
    assert outcome.evidence["button"] == "right"


async def test_mouse_scroll_bounded(tmp_path) -> None:
    tool, backend = make()
    outcome = await tool.run("mouse_scroll", {"amount": -3}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.scrolled == [-3]
    bad = await tool.run("mouse_scroll", {"amount": 11}, ctx(tmp_path))
    assert bad.status is ExecutionStatus.ERRORED


# ----------------------------------------------------------- keyboard ------


async def test_type_text_records_and_bounds(tmp_path) -> None:
    tool, backend = make()
    outcome = await tool.run("type_text", {"text": "hello"}, ctx(tmp_path))
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert backend.typed == ["hello"]
    assert outcome.evidence["typed_chars"] == 5
    bad = await tool.run("type_text", {"text": ""}, ctx(tmp_path))
    assert bad.status is ExecutionStatus.ERRORED


async def test_press_key_normalizes_and_records(tmp_path) -> None:
    tool, backend = make()
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
    backend.windows = [
        {"handle": i, "title": f"{'x' * 300} #{i}"} for i in range(MAX_WINDOWS + 5)
    ]
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
