"""Desktop platform adapters (SPEC §26, §53 Phase 12).

`DesktopBackend` is the narrow control surface the tool gateway calls.
The Windows implementation lives in `desktop.win32` (ctypes user32/gdi32
directly — no third-party automation framework, no shell, no network).
When the platform or the interactive session cannot provide desktop
control, adapters report a structured UNAVAILABLE result instead of
faking success (SPEC §56.14 honest absence).

`FakeDesktopBackend` is the deterministic in-memory adapter for tests
(SPEC §10 fake-adapter rule): cursor position, typed keys, windows, and
clicks are scripted and recorded; no real OS calls.
"""

from __future__ import annotations

from typing import Any, NoReturn


class DesktopUnavailable(OSError):
    """No usable desktop control surface on this platform/session."""


class DesktopBackend:
    """Platform adapter surface. Implementations are DATA/CONTROL adapters
    only — no authority, no policy, no task-level judgments."""

    def screen_size(self) -> tuple[int, int]:  # pragma: no cover - protocol
        raise NotImplementedError

    def capture(self, width: int, height: int) -> bytes:  # pragma: no cover - protocol
        """Return raw 32bpp BGRA pixel rows for the primary screen."""
        raise NotImplementedError

    def mouse_move(self, x: int, y: int) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def mouse_click(self, x: int, y: int, button: str, clicks: int) -> None:  # pragma: no cover
        raise NotImplementedError

    def mouse_scroll(self, amount: int) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def type_text(self, text: str) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def press_key(self, key: str) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def list_windows(self, limit: int) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        raise NotImplementedError

    def foreground_window(self) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError

    def focus_window(self, title: str) -> dict[str, Any]:  # pragma: no cover - protocol
        raise NotImplementedError


class UnavailableDesktopBackend(DesktopBackend):
    """Honest absence: raises a structured, typed unavailability error.

    The tool renders this as an honest failed ToolOutcome — never a fake
    observation or a fabricated success (SPEC §56.14)."""

    engine_id = "unavailable"

    def _refuse(self) -> NoReturn:
        raise DesktopUnavailable(
            "desktop control unavailable: no interactive desktop session on this platform"
        )

    def screen_size(self) -> tuple[int, int]:
        self._refuse()

    def capture(self, width: int, height: int) -> bytes:
        self._refuse()

    def mouse_move(self, x: int, y: int) -> None:
        self._refuse()

    def mouse_click(self, x: int, y: int, button: str, clicks: int) -> None:
        self._refuse()

    def mouse_scroll(self, amount: int) -> None:
        self._refuse()

    def type_text(self, text: str) -> None:
        self._refuse()

    def press_key(self, key: str) -> None:
        self._refuse()

    def list_windows(self, limit: int) -> list[dict[str, Any]]:
        self._refuse()

    def foreground_window(self) -> dict[str, Any]:
        self._refuse()

    def focus_window(self, title: str) -> dict[str, Any]:
        self._refuse()


def default_backend() -> DesktopBackend:
    """Platform-selected adapter: real Win32 on Windows, honest absence
    elsewhere. Selection is configuration, never authority."""
    import sys

    if sys.platform == "win32":
        from nomadicos.desktop.win32 import Win32DesktopBackend

        return Win32DesktopBackend()
    return UnavailableDesktopBackend()


class FakeDesktopBackend(DesktopBackend):
    """Deterministic in-memory adapter for tests (SPEC §10 fake adapters).

    No real OS calls: cursor position, typed keys, windows, and captures
    are scripted and recorded for assertions."""

    def __init__(self, width: int = 1920, height: int = 1080) -> None:
        self.width = width
        self.height = height
        self.cursor = (0, 0)
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.scrolled: list[int] = []
        self.clicks: list[tuple[int, int, str, int]] = []
        self.moves: list[tuple[int, int]] = []
        self.captures = 0
        self.windows: list[dict[str, Any]] = [
            {"handle": 1, "title": "Untitled - Notepad"},
            {"handle": 2, "title": "NomadicOS Console"},
        ]
        self.focused: str | None = None

    def screen_size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def capture(self, width: int, height: int) -> bytes:
        self.captures += 1
        row = b"\x00\x00\x40\xff" * width
        return row * height

    def mouse_move(self, x: int, y: int) -> None:
        self.cursor = (x, y)

    def mouse_click(self, x: int, y: int, button: str, clicks: int) -> None:
        self.cursor = (x, y)
        self.clicks.append((x, y, button, clicks))

    def mouse_scroll(self, amount: int) -> None:
        self.scrolled.append(amount)

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def press_key(self, key: str) -> None:
        self.keys.append(key)

    def list_windows(self, limit: int) -> list[dict[str, Any]]:
        return self.windows[:limit]

    def foreground_window(self) -> dict[str, Any]:
        return dict(self.windows[0]) if self.windows else {"handle": 0, "title": ""}

    def focus_window(self, title: str) -> dict[str, Any]:
        for w in self.windows:
            if title.lower() in str(w.get("title", "")).lower():
                self.focused = str(w["title"])
                return {"focused": True, "handle": w["handle"], "title": w["title"]}
        return {"focused": False, "reason": "window not found"}


def is_win32_backend(backend: DesktopBackend) -> bool:
    return getattr(backend, "engine_id", "") == "win32"
