"""Desktop control contracts (SPEC §26, §53 Phase 12) — DATA + typed boundaries.

Typed arguments, deterministic validation, and bounded results for every
desktop capability. Constants here are the security bounds:

- coordinates are validated against the REAL screen size; out-of-bounds
  input is REJECTED (never silently clamped);
- key names are a closed vocabulary; malformed keys are rejected;
- typed text, hotkeys, scroll, window/process listings and screenshots are
  all bounded;
- UI/window/process metadata is untrusted DATA: a window title can never
  grant permission or answer an owner conflict.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TYPE_TEXT = 2000
MAX_HOTKEY_PARTS = 4
MAX_KEY_CHARS = 16
MAX_SCROLL = 10
MAX_WINDOWS = 50
MAX_PROCESSES = 100
MAX_CAPTURE_DIM = 3840
MAX_MOUSE_COORD = 100_000
MAX_TITLE_CHARS = 200
MAX_CLICKS = 3

MOUSE_BUTTONS = ("left", "right", "middle")

#: closed keyboard vocabulary (SPEC §26 desktop.keyboard is typed intent,
#: never shell syntax); single alphanumerics resolve via the OS key map
NAMED_KEYS = frozenset(
    {
        "enter",
        "tab",
        "space",
        "backspace",
        "delete",
        "esc",
        "home",
        "end",
        "pageup",
        "pagedown",
        "insert",
        "up",
        "down",
        "left",
        "right",
        "shift",
        "ctrl",
        "alt",
        "win",
        "capslock",
        *tuple(f"f{n}" for n in range(1, 13)),
    }
)
_MODIFIERS = frozenset({"shift", "ctrl", "alt", "win"})


class _Args(BaseModel):
    """Base for typed desktop arguments; unknown fields fail closed."""

    model_config = ConfigDict(extra="forbid")


class ScreenshotArgs(_Args):
    """Full primary-screen capture; dimensions checked at execution time."""


class MouseMoveArgs(_Args):
    x: int = Field(ge=0, le=MAX_MOUSE_COORD)
    y: int = Field(ge=0, le=MAX_MOUSE_COORD)


class MouseClickArgs(MouseMoveArgs):
    button: str = "left"
    clicks: int = Field(default=1, ge=1, le=MAX_CLICKS)

    @field_validator("button")
    @classmethod
    def _button(cls, v: str) -> str:
        if v not in MOUSE_BUTTONS:
            raise ValueError(f"unsupported mouse button {v!r}")
        return v


class MouseScrollArgs(_Args):
    amount: int = Field(ge=-MAX_SCROLL, le=MAX_SCROLL)


class TypeTextArgs(_Args):
    text: str = Field(min_length=1, max_length=MAX_TYPE_TEXT)


class PressKeyArgs(_Args):
    """A key or a '+'-joined hotkey from the closed vocabulary (SPEC §26)."""

    key: str

    @field_validator("key")
    @classmethod
    def _vocab(cls, v: str) -> str:
        return "+".join(validate_hotkey(v))


class ListWindowsArgs(_Args):
    limit: int = Field(default=MAX_WINDOWS, ge=1, le=MAX_WINDOWS)


class ForegroundWindowArgs(_Args):
    pass


class FocusWindowArgs(_Args):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)


def validate_key(key: str) -> str:
    if not valid_key_name(key):
        raise ValueError(f"unsupported key {key!r}")
    return key.lower()


def valid_key_name(key: str) -> bool:
    if not key or len(key) > MAX_KEY_CHARS:
        return False
    low = key.lower()
    return low in NAMED_KEYS or (len(key) == 1 and key.isalnum())


def validate_hotkey(hotkey: str) -> list[str]:
    parts = [p for p in hotkey.split("+") if p.strip()]
    if not 1 <= len(parts) <= MAX_HOTKEY_PARTS:
        raise ValueError(f"hotkey must have 1..{MAX_HOTKEY_PARTS} parts")
    return [validate_key(part) for part in parts]


def validate_point(x: int, y: int, width: int, height: int) -> None:
    """Coordinates MUST be inside the real screen bounds — never clamped."""
    if not 0 <= int(x) < width or not 0 <= int(y) < max(int(height), 1):
        raise ValueError(f"coordinate ({x}, {y}) outside screen {width}x{height}")
