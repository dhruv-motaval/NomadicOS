"""Desktop typed-contract validation (SPEC §26): closed vocabulary, hard
bounds, no silent clamping, malformed input fails closed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.desktop.contracts import (
    MAX_TYPE_TEXT,
    MAX_WINDOWS,
    FocusWindowArgs,
    ListWindowsArgs,
    MouseClickArgs,
    MouseMoveArgs,
    MouseScrollArgs,
    PressKeyArgs,
    TypeTextArgs,
    validate_hotkey,
    validate_key,
    validate_point,
)

# ------------------------------------------------------------------ keys ---


def test_named_keys_accepted() -> None:
    for key in ("enter", "ctrl", "esc", "f12", "pagedown"):
        assert validate_key(key) == key


def test_single_alnum_keys_accepted_and_normalized() -> None:
    assert validate_key("A") == "a"
    assert validate_key("5") == "5"


def test_unknown_words_rejected() -> None:
    for bad in ("abc", "escape", "foo", "ctrl;delete", "delete-all", ""):
        with pytest.raises(ValueError):
            validate_key(bad)


def test_hotkey_parts_bounds() -> None:
    assert validate_hotkey("ctrl+alt+delete") == ["ctrl", "alt", "delete"]
    with pytest.raises(ValueError, match="parts"):
        validate_hotkey("a+b+c+d+e")


def test_hotkey_rejects_unknown_component() -> None:
    with pytest.raises(ValueError, match="unsupported key"):
        validate_hotkey("ctrl+reformat-disk")


# --------------------------------------------------------------- mouse -----


def test_coordinates_validated_never_clamped() -> None:
    validate_point(0, 0, 1920, 1080)
    validate_point(1919, 1079, 1920, 1080)
    with pytest.raises(ValueError):
        validate_point(1920, 0, 1920, 1080)
    with pytest.raises(ValueError, match="outside screen"):
        validate_point(-1, 5, 1920, 1080)
    with pytest.raises(ValueError, match="outside screen"):
        validate_point(5, 1080, 1920, 1080)
    with pytest.raises(ValueError):
        validate_point(0, -1, 1920, 1080)
    with pytest.raises(ValueError):
        validate_point(0, 0, 0, 0)


def test_mouse_button_is_closed_enum() -> None:
    assert MouseClickArgs(x=1, y=1, button="middle").button == "middle"
    with pytest.raises(ValidationError):
        MouseClickArgs(x=1, y=1, button="nuke")


def test_clicks_bounded() -> None:
    assert MouseClickArgs(x=1, y=1, clicks=3).clicks == 3
    with pytest.raises(ValidationError):
        MouseClickArgs(x=1, y=1, clicks=4)


def test_coordinates_reject_negative_and_out_of_model_range() -> None:
    with pytest.raises(ValidationError):
        MouseMoveArgs(x=-1, y=0)
    with pytest.raises(ValidationError):
        MouseMoveArgs(x=100_001, y=0)
    assert MouseMoveArgs(x=100_000 - 1, y=99_999)


def test_scroll_bounded() -> None:
    assert MouseScrollArgs(amount=10).amount == 10
    assert MouseScrollArgs(amount=-10).amount == -10
    with pytest.raises(ValidationError):
        MouseScrollArgs(amount=11)
    with pytest.raises(ValidationError):
        MouseScrollArgs(amount=0.5)


# ------------------------------------------------------------------ text ---


def test_text_bounds() -> None:
    assert TypeTextArgs(text="x" * MAX_TYPE_TEXT) is not None
    with pytest.raises(ValidationError):
        TypeTextArgs(text="")
    with pytest.raises(ValidationError):
        TypeTextArgs(text="x" * (MAX_TYPE_TEXT + 1))


def test_press_key_normalizes_vocabulary() -> None:
    assert PressKeyArgs(key="CTRL+S").key == "ctrl+s"
    assert PressKeyArgs(key="F12").key == "f12"
    with pytest.raises(ValidationError):
        PressKeyArgs(key="ctrl;alt")
    with pytest.raises(ValidationError):
        PressKeyArgs(key="a+b+c+d+e")
    with pytest.raises(ValidationError):
        PressKeyArgs(key="format c:")


# --------------------------------------------------------------- windows ---


def test_window_title_bounded() -> None:
    assert FocusWindowArgs(title="t" * 200).title == "t" * 200
    with pytest.raises(ValidationError):
        FocusWindowArgs(title="t" * 201)
    with pytest.raises(ValidationError):
        FocusWindowArgs(title="")


def test_list_windows_limit_bounded() -> None:
    assert ListWindowsArgs().limit == MAX_WINDOWS
    assert ListWindowsArgs(limit=1).limit == 1
    with pytest.raises(ValidationError):
        ListWindowsArgs(limit=MAX_WINDOWS + 1)


def test_unknown_args_fail_closed() -> None:
    with pytest.raises(ValidationError):
        MouseMoveArgs(x=1, y=2, authorized=True)
    with pytest.raises(ValidationError):
        PressKeyArgs(key="ctrl", bypass="yes")


def test_zero_screen_rejects_points() -> None:
    with pytest.raises(ValueError):
        validate_point(0, 0, 0, 0)
