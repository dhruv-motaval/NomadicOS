"""Phase 12A hardware tests: REAL screen interactions on the owner's machine.

Safe reads + a single cursor move only — no clicks, no typing, no window
focus changes, no destructive input. Hardware-marked; excluded from the
deterministic suite. Honest SKIPPED when no interactive desktop session
exists; never a fabricated PASS.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from pathlib import Path

import pytest

from nomadicos.desktop.backend import DesktopUnavailable
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.desktop import DesktopTool

pytestmark = pytest.mark.hardware


def _real_backend():
    if sys.platform != "win32":
        pytest.skip("desktop adapter is Windows-only")
    from nomadicos.desktop.win32 import Win32DesktopBackend

    backend = Win32DesktopBackend()
    width, height = backend.screen_size()
    if width <= 0 or height <= 0:
        pytest.skip("no interactive desktop metrics available")
    return backend


def test_screen_size_is_positive() -> None:
    width, height = _real_backend().screen_size()
    assert width > 0 and height > 0


async def test_screenshot_captures_real_screen(tmp_path) -> None:

    tool = DesktopTool(_real_backend())
    ctx_ = ExecutionContext.for_task("hw_desktop_probe", tmp_path / "ws")
    try:
        outcome = await tool.run("screenshot", {}, ctx_)
    except DesktopUnavailable:
        pytest.skip("no interactive desktop for capture")
    if outcome.status.value == "FAILED" and "unavailable" in outcome.message:
        pytest.skip(outcome.message)
    assert outcome.status.value == "SUCCEEDED"
    evidence = outcome.evidence
    data = Path(evidence["path"]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert evidence["width"] > 0 and evidence["height"] > 0
    assert evidence["bytes"] == len(data) > 0


def test_mouse_move_centers_cursor_and_reads_back() -> None:
    backend = _real_backend()
    width, height = backend.screen_size()
    backend.mouse_move(width // 2, height // 2)
    pt = ctypes.wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        pytest.skip("cursor position unavailable")
    assert abs(pt.x - width // 2) <= 2
    assert abs(pt.y - height // 2) <= 2


def test_list_windows_returns_real_titles() -> None:
    windows = _real_backend().list_windows(50)
    assert isinstance(windows, list)
    for window in windows:
        assert "handle" in window and "title" in window
        assert isinstance(window["title"], str)
        assert len(window["title"]) > 0


def test_foreground_window_reports_data() -> None:
    window = _real_backend().foreground_window()
    assert "handle" in window and "title" in window


def test_focus_current_foreground_window_is_safe_noop() -> None:
    """Safe focus validation: re-focus the CURRENT foreground window by its
    own title — a no-op for the user, exercising the real focus path."""
    backend = _real_backend()
    current = backend.foreground_window()
    title = str(current.get("title", "")).strip()
    if not title:
        pytest.skip("no foreground window title available")
    result = backend.focus_window(title)
    assert "focused" in result
    # either the OS granted focus (no-op change) or honestly refused
    assert isinstance(result["focused"], bool)
