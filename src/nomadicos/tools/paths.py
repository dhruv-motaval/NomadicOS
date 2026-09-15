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


def resolve_scoped(raw: str, ctx: ExecutionContext) -> Path:
    """Resolve a model-supplied path against the task scope.

    Raises PathDenied when the RESOLVED location escapes the workspace and
    the context is not FULL_PC. Absolute paths are allowed under FULL_PC.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathDenied("empty path")
    candidate = Path(raw).expanduser()
    _reject_invalid(candidate)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    candidate = Path(os.path.normcase(str(candidate)))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:  # e.g. symlink loops
        raise PathDenied(f"unresolvable path: {exc}") from exc
    if not ctx.full_pc:
        root = Path(os.path.normcase(str(ctx.workspace.resolve(strict=False))))
        if os.path.commonpath([str(root), str(resolved)]) != str(root):
            raise PathDenied(f"{resolved} escapes task workspace {root}")
    return resolved
