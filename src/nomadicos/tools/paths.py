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


def resolve_scoped(raw: str, ctx: ExecutionContext) -> Path:
    """Resolve a model-supplied path against the task scope.

    Raises PathDenied when the RESOLVED location escapes the workspace and
    the context is not FULL_PC, or lands in protected owner state (any
    scope). Absolute paths are allowed under FULL_PC.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathDenied("empty path")
    # Reject POSIX-style absolute paths on Windows (they are not valid Windows
    # paths and would resolve to the drive root, e.g. /usr/bin -> C:\usr\bin).
    if os.name == "nt" and raw.startswith("/"):
        raise PathDenied(f"POSIX absolute path not allowed on Windows: {raw}")
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
