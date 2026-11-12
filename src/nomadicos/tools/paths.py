"""Path handling for Windows + POSIX (SPEC §23, §6 confinement boundary).

Confinement uses RESOLVED real paths and segment containment — never string
prefix checks — so ``C:\\workspace`` cannot be escaped via
``C:\\workspace-secret`` and junctions/symlinks cannot launder a path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath

from nomadicos.kernel.errors import AuthorizationDenied
from nomadicos.tools.context import ExecutionContext

_INVALID_WIN = re.compile(r'[<>:"|?*\x00]')


class PathDenied(AuthorizationDenied):
    """Escape attempt / invalid path: an AUTHORIZATION-shaped refusal."""


#: common POSIX absolute prefixes, mapped to their Windows equivalents.
#: The translation is purely lexical; confinement is enforced on the
#: RESOLVED Windows path afterwards, exactly as for native paths.
_POSIX_TO_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("/tmp", "TEMP", r"C:\Windows\Temp"),
    ("/var/tmp", "TEMP", r"C:\Windows\Temp"),
    ("/usr/bin", "SYSTEMROOT", r"C:\Windows"),
    ("/usr/local/bin", "SYSTEMROOT", r"C:\Windows"),
    ("/home", "USERPROFILE", r"C:\Users"),
    ("/root", "USERPROFILE", r"C:\Users"),
)


def _to_windows_abs(raw: str) -> Path:
    """Translate a POSIX-style absolute path to its Windows equivalent.

    LLM-authored paths routinely follow POSIX conventions (/tmp/x, /usr/bin)
    even on Windows. A blanket refusal makes legitimate intents fail closed
    for a purely lexical reason. Translation keeps confinement intact because
    the WINDOWS path is then resolved and segment-checked exactly like any
    other absolute path (SPEC §23: resolved paths, never string prefixes)."""
    for prefix, env_var, default in _POSIX_TO_WINDOWS:
        if raw == prefix or raw.startswith(f"{prefix}/"):
            rest = raw[len(prefix) :].lstrip("/")
            return Path(os.environ.get(env_var, default)) / rest
    return Path(raw)


def _reject_invalid(pure: PurePath) -> None:
    if "\x00" in str(pure):
        raise PathDenied("null byte in path")
    if os.name == "nt":
        for part in pure.parts[1:]:  # skip drive "C:\\"
            if _INVALID_WIN.search(part):
                raise PathDenied(f"invalid characters in path component {part!r}")


def normalize_root(path: str | Path) -> Path:
    """Resolved, case-normalized form used for every containment check."""
    return Path(os.path.normcase(str(Path(path).resolve(strict=False))))


def is_within(root: Path, path: Path) -> bool:
    """Segment containment of two normalized paths (False across drives)."""
    try:
        return os.path.commonpath([str(root), str(path)]) == str(root)
    except ValueError:  # different drives on Windows
        return False


def deny_protected(resolved: Path, ctx: ExecutionContext) -> None:
    """Refuse owner-authority state for EVERY scope, FULL_PC included
    (SPEC §4, §56.5): no task may rewrite the grant it acts under."""
    for root in ctx.protected:
        if is_within(root, resolved):
            raise PathDenied(f"{resolved} is protected NomadicOS owner state")


def _confine(resolved: Path, ctx: ExecutionContext) -> Path:
    if not ctx.full_pc:
        root = Path(os.path.normcase(str(ctx.workspace.resolve(strict=False))))
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise PathDenied(f"{resolved} escapes task workspace {root}")
    deny_protected(resolved, ctx)
    return resolved


def _resolve_windows(candidate: Path, ctx: ExecutionContext) -> Path:
    """Run the SAME invalid-char + resolved-path confinement checks for a
    Windows path candidate (native or POSIX-translated)."""
    _reject_invalid(candidate)
    candidate = Path(os.path.normcase(str(candidate)))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:  # e.g. symlink loops
        raise PathDenied(f"unresolvable path: {exc}") from exc
    return _confine(resolved, ctx)


def resolve_scoped(raw: str, ctx: ExecutionContext) -> Path:
    """Resolve a model-supplied path against the task scope.

    Raises PathDenied when the RESOLVED location escapes the workspace and
    the context is not FULL_PC, or lands in protected owner state (any
    scope). Absolute paths are allowed under FULL_PC.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathDenied("empty path")
    # POSIX-style absolute paths on Windows: translate to their Windows
    # equivalent, then confine the RESOLVED Windows path below. They are not
    # valid Windows paths natively (/usr/bin would resolve to C:\usr\bin);
    # refusing them outright fails closed for a lexical reason, not a scope
    # reason. The translated path goes through the SAME invalid-char and
    # containment checks afterwards.
    if os.name == "nt" and raw.startswith("/"):
        return _resolve_windows(_to_windows_abs(raw), ctx)
    candidate = Path(raw).expanduser()
    _reject_invalid(candidate)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    candidate = Path(os.path.normcase(str(candidate)))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:  # e.g. symlink loops
        raise PathDenied(f"unresolvable path: {exc}") from exc
    return _confine(resolved, ctx)
