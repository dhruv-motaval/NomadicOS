"""Windows desktop adapter: ctypes user32/gdi32 (SPEC §26, §53 Phase 12).

No third-party automation frameworks and no process spawning anywhere in
this module: input injection, window enumeration, and screen capture go
through ctypes against user32/gdi32. The module performs NO authorization
and never invents success - OS failures become structured exceptions that
the tool gateway renders as honest ToolOutcome failures.
"""

from __future__ import annotations

import ctypes
import struct
from typing import Any

from nomadicos.desktop.backend import DesktopBackend, DesktopUnavailable

KEYUP = 0x0002
SRCCOPY = 0x00CC0020
WHEEL = 0x0800
MAX_ENUM_WINDOWS = 200
MOUSE = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}

VK = {
    "enter": 0x0D, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "esc": 0x1B, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B,
    "capslock": 0x14,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def _window_title(user32: Any, ct: Any, hwnd: int) -> str:
    size = int(user32.GetWindowTextLengthW(hwnd))
    if size <= 0:
        return ""
    buf = ct.create_unicode_buffer(size + 1)
    user32.GetWindowTextW(hwnd, buf, size + 1)
    return buf.value


def _enum_windows(user32: Any, ct: Any, limit: int) -> list[dict[str, int | str]]:
    results: list[dict[str, int | str]] = []
    proc = ct.WINFUNCTYPE(ct.c_bool, ct.c_void_p, ct.c_void_p)

    def on_window(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            title = _window_title(user32, ct, hwnd)
            if title.strip():
                results.append({"handle": int(hwnd), "title": title})
        return len(results) < limit

    user32.EnumWindows(proc(on_window), 0)
    return results[:limit]


def _bitmap_info(width: int, height: int) -> bytes:
    """BITMAPINFOHEADER: negative height = top-down rows."""
    return struct.pack(
        "<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0
    )


class Win32DesktopBackend(DesktopBackend):
    """Windows 11 adapter: no third-party frameworks, no process spawning."""

    engine_id = "win32"

    def __init__(self) -> None:
        self._ct = ctypes
        self._user32 = ctypes.windll.user32
        self._gdi32 = ctypes.windll.gdi32

    def screen_size(self) -> tuple[int, int]:
        return (int(self._user32.GetSystemMetrics(0)), int(self._user32.GetSystemMetrics(1)))

    def capture(self, width: int, height: int) -> bytes:
        user32, gdi32 = self._user32, self._gdi32
        dc = user32.GetDC(0)
        if not dc:
            raise DesktopUnavailable("GetDC failed: no interactive desktop")
        mem = gdi32.CreateCompatibleDC(dc)
        bmp = gdi32.CreateCompatibleBitmap(dc, width, height)
        try:
            gdi32.SelectObject(mem, bmp)
            gdi32.BitBlt(mem, 0, 0, width, height, dc, 0, 0, SRCCOPY)
            bmi = _bitmap_info(width, height)
            buf = self._ct.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(mem, bmp, 0, height, buf, bmi, 0) != height:
                raise OSError("screen capture failed")
            return bytes(buf.raw)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(0, dc)

    def mouse_move(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(int(x), int(y))

    def mouse_click(self, x: int, y: int, button: str, clicks: int) -> None:
        down, up = MOUSE[button]
        self._user32.SetCursorPos(int(x), int(y))
        for _ in range(int(clicks)):
            self._user32.mouse_event(down, 0, 0, 0, 0)
            self._user32.mouse_event(up, 0, 0, 0, 0)

    def mouse_scroll(self, amount: int) -> None:
        self._user32.mouse_event(WHEEL, 0, 0, int(amount) * 120, 0)

    def type_text(self, text: str) -> None:
        for ch in text:
            scan = int(self._user32.VkKeyScanW(ord(ch)))
            if scan == -1:
                raise ValueError(f"untypeable character {ch!r}")
            vk = scan & 0xFF
            needs_shift = bool((scan >> 8) & 1)
            if needs_shift:
                self._user32.keybd_event(0x10, 0, 0, 0)
            self._user32.keybd_event(vk, 0, 0, 0)
            self._user32.keybd_event(vk, 0, KEYUP, 0)
            if needs_shift:
                self._user32.keybd_event(0x10, 0, KEYUP, 0)

    def press_key(self, key: str) -> None:
        """Press a key or a '+'-joined hotkey: modifiers are held, the final
        key is tapped, and modifiers release in reverse order."""
        parts = key.split("+")
        held = [self._vk(p) for p in parts[:-1]]
        vk = self._vk(parts[-1])
        for code in held:
            self._user32.keybd_event(code, 0, 0, 0)
        self._user32.keybd_event(vk, 0, 0, 0)
        self._user32.keybd_event(vk, 0, KEYUP, 0)
        for code in reversed(held):
            self._user32.keybd_event(code, 0, KEYUP, 0)

    def _vk(self, key: str) -> int:
        if key in VK:
            return VK[key]
        if len(key) == 1 and key.isalnum():
            return int(ord(key.upper()))
        raise ValueError(f"unsupported key {key!r}")

    def list_windows(self, limit: int) -> list[dict[str, int | str]]:
        return _enum_windows(self._user32, self._ct, limit)

    def foreground_window(self) -> dict[str, Any]:
        hwnd = int(self._user32.GetForegroundWindow() or 0)
        if not hwnd:
            return {"handle": 0, "title": ""}
        return {"handle": hwnd, "title": _window_title(self._user32, self._ct, hwnd)}

    def focus_window(self, title: str) -> dict[str, Any]:
        for w in _enum_windows(self._user32, self._ct, MAX_ENUM_WINDOWS):
            if title.lower() in str(w["title"]).lower():
                ok = bool(self._user32.SetForegroundWindow(w["handle"]))
                return {"focused": ok, "handle": w["handle"], "title": str(w["title"])}
        return {"focused": False, "reason": "window not found"}
