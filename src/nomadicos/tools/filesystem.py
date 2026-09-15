"""Filesystem brick (SPEC §23): typed, attributed, evidence-producing ops.

Every state-changing op records: authorized-action correlation (added by the
executor), sha256 before/after, bytes. IO problems return structured failed
outcomes; path-escape problems raise PathDenied (an authorization refusal).
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure, ToolError
from nomadicos.tools.base import Tool, ToolOutcome
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import resolve_scoped

MAX_READ_CHARS = 200_000


def _sha(path: Path | None) -> str | None:
    try:
        if path is None or not path.is_file():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class ReadArgs(BaseModel):
    path: str


class WriteArgs(BaseModel):
    path: str
    content: str
    append: bool = False


class CreateArgs(BaseModel):
    path: str
    content: str = ""


class DeleteArgs(BaseModel):
    path: str


class ExistsArgs(BaseModel):
    path: str


class ListArgs(BaseModel):
    directory: str = "."


class MkdirArgs(BaseModel):
    directory: str


class MoveArgs(BaseModel):
    source: str
    target: str


class FilesystemTool(Tool):
    name = "filesystem"
    ops: ClassVar[dict[str, type[BaseModel]]] = {
        "read": ReadArgs,
        "write": WriteArgs,
        "create": CreateArgs,
        "delete": DeleteArgs,
        "exists": ExistsArgs,
        "list": ListArgs,
        "mkdir": MkdirArgs,
        "move": MoveArgs,
    }

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        started = time.monotonic()
        handler = getattr(self, f"_op_{operation}")
        typed = self.ops[operation].model_validate(args)
        outcome = handler(typed, ctx)
        outcome.duration_s = round(time.monotonic() - started, 4)
        return outcome

    # ------------------------------------------------------------- ops ----

    def _op_read(self, a: ReadArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.path, ctx)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"not found: {path}",
                evidence={"path": str(path)},
            )
        except IsADirectoryError:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"is a directory: {path}",
            )
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        truncated = len(text) > MAX_READ_CHARS
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": _sha(path),
                "content": text[:MAX_READ_CHARS],
                "truncated": truncated,
            },
        )

    def _op_write(self, a: WriteArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.path, ctx)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if a.append:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(a.content)
            else:
                tmp = path.with_name(path.name + ".tmp-nomadic")
                tmp.write_text(a.content, encoding="utf-8")
                replace = _atomic_replace(tmp, path)
                if replace is not None:
                    return ToolOutcome(
                        status=ExecutionStatus.ERRORED, failure=Failure.TOOL_ERROR, message=replace
                    )
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={
                "path": str(path),
                "sha256": _sha(path),
                "bytes_written": len(a.content),
                "append": a.append,
            },
        )

    def _op_create(self, a: CreateArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.path, ctx)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(a.content)
        except FileExistsError:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"already exists: {path}",
            )
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED, evidence={"path": str(path), "sha256": _sha(path)}
        )

    def _op_delete(self, a: DeleteArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.path, ctx)
        before = _sha(path) if path.is_file() else None
        try:
            path.unlink(missing_ok=False)
        except FileNotFoundError:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"not found: {path}",
            )
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"path": str(path), "sha256_before": before, "exists_after": path.exists()},
        )

    def _op_exists(self, a: ExistsArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.path, ctx)
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED, evidence={"path": str(path), "exists": path.exists()}
        )

    def _op_list(self, a: ListArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.directory, ctx)
        try:
            entries = [
                {
                    "name": p.name,
                    "is_dir": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else None,
                }
                for p in sorted(path.iterdir())
            ]
        except FileNotFoundError:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"not found: {path}",
            )
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"directory": str(path), "entries": entries[:500], "count": len(entries)},
        )

    def _op_mkdir(self, a: MkdirArgs, ctx: ExecutionContext) -> ToolOutcome:
        path = resolve_scoped(a.directory, ctx)
        try:
            path.mkdir(parents=True)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                return ToolOutcome(
                    status=ExecutionStatus.FAILED,
                    failure=Failure.ACTION_FAILED,
                    message=f"already exists: {path}",
                )
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(status=ExecutionStatus.SUCCEEDED, evidence={"path": str(path)})

    def _op_move(self, a: MoveArgs, ctx: ExecutionContext) -> ToolOutcome:
        src = resolve_scoped(a.source, ctx)
        dst = resolve_scoped(a.target, ctx)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"{type(exc).__name__}: {exc}",
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            evidence={"from": str(src), "to": str(dst), "sha256": _sha(dst)},
        )


def _atomic_replace(tmp, path) -> str | None:
    try:
        import os

        os.replace(tmp, path)
        return None
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return f"atomic replace failed: {exc}"


class FilesystemError(ToolError):
    """Kept for callers that special-case fs failures."""
