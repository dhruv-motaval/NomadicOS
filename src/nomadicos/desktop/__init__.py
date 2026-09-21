"""Desktop control brick (SPEC §26, §53 Phase 12).

Typed capability contracts + replaceable platform adapters behind the
existing executor. Desktop observations and effects are DATA; authority,
policy, and task-level judgments live upstream of this package.
"""

from nomadicos.desktop.artifacts import encode_png
from nomadicos.desktop.backend import (
    DesktopBackend,
    DesktopUnavailable,
    FakeDesktopBackend,
    UnavailableDesktopBackend,
    default_backend,
)
from nomadicos.desktop.contracts import (
    MAX_CAPTURE_DIM,
    MAX_CLICKS,
    MAX_HOTKEY_PARTS,
    MAX_KEY_CHARS,
    MAX_MOUSE_COORD,
    MAX_PROCESSES,
    MAX_SCROLL,
    MAX_TITLE_CHARS,
    MAX_TYPE_TEXT,
    MAX_WINDOWS,
    MOUSE_BUTTONS,
    NAMED_KEYS,
    validate_hotkey,
    validate_key,
    validate_point,
)

__all__ = [
    "DesktopBackend",
    "DesktopUnavailable",
    "MAX_CAPTURE_DIM",
    "MAX_CLICKS",
    "MAX_HOTKEY_PARTS",
    "MAX_KEY_CHARS",
    "MAX_MOUSE_COORD",
    "MAX_PROCESSES",
    "MAX_SCROLL",
    "MAX_TITLE_CHARS",
    "MAX_TYPE_TEXT",
    "MAX_WINDOWS",
    "MOUSE_BUTTONS",
    "NAMED_KEYS",
    "FakeDesktopBackend",
    "UnavailableDesktopBackend",
    "default_backend",
    "encode_png",
    "validate_hotkey",
    "validate_key",
    "validate_point",
]
