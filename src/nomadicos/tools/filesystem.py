"""Filesystem tool (BP §13, §92, §139, §193; ADR-0018).

Security properties:
- path normalization + traversal rejection (BP §193)
- classification-aware: sensitive paths require the gate to ask (via classifier)
- dry-run for state-changing/destructive operations (BP §139)
- evidence-bearing results: existence + hash change (BP §143/§146)
"""

import hashlib
from pathlib import Path
from typing import Any

from nomadicos.core.errors import ValidationError
from nomadicos.security.classifier import classify
from nomadicos.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
    validate_against_schema,
)

FILESYSTEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["read", "write", "delete", "list"]},
        "path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "content": {"type": "string", "maxLength": 1_000_000},
    },
    "required": ["action", "path"],
    "additionalProperties": False,
}

MAX_READ_BYTES = 512 * 1024


def safe_resolve(path: str, workspace_root: str | None) -> Path:
    """Normalize and confine the path (BP §193).

    Relative paths resolve against the task workspace (BP §92); absolute paths
    must stay inside it; traversal segments are rejected outright.

    Deterministic normalization (STEP 3 review): a model may hand back the
    workspace's own directory name as a prefix (observed: writing
    'data/task-workspaces/ir-live.txt' produced
    'data/task-workspaces/data/task-workspaces/ir-live.txt'). Leading
    segments that echo the root's own tail are stripped — repeated, bounded.
    """
    raw = Path(path)
    if workspace_root:
        root = Path(workspace_root).resolve()
        candidate = raw if raw.is_absolute() else root / raw
        # strip echoed workspace tail segments: data/task-workspaces/x -> x
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            relative = None
        if relative is not None:
            parts = list(relative.parts)
            root_tail = list(root.parts)
            changed = True
            while changed and parts:
                changed = False
                for take in range(min(len(parts), len(root_tail)), 0, -1):
                    if parts[:take] == root_tail[-take:] or (
                        take == 1 and parts[0].lower() == root_tail[-1].lower()
                    ):
                        parts = parts[take:]
                        changed = True
                        break
            candidate = root.joinpath(*parts) if parts else root
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ValidationError(
                f"path escapes the task workspace: {path}",
                context={"path_kind": "escape"},
            )
    else:
        resolved = raw.resolve()
    if ".." in raw.parts:
        raise ValidationError("path traversal segments are not allowed")
    return resolved


class FilesystemTool(Tool):
    """READ (list/read) + STATE_CHANGING (write) + DESTRUCTIVE (delete) in one
    tool surface; the Security Gate sees the effective risk per action."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self._workspace_root = workspace_root

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="filesystem",
            description="read, write, list, and delete files inside the task workspace",
            risk=ToolRisk.STATE_CHANGING,  # worst case; gate sees action-aware risk below
            arguments_schema=FILESYSTEM_SCHEMA,
            evidence_kind='filesystem',
            side_effects=["creates files", "modifies files", "deletes files"],
            supports_dry_run=True,
        )

    def effective_risk(self, arguments: dict[str, Any]) -> ToolRisk:
        action = arguments.get("action", "read")
        return {
            "read": ToolRisk.READ_ONLY,
            "list": ToolRisk.READ_ONLY,
            "write": ToolRisk.STATE_CHANGING,
            "delete": ToolRisk.DESTRUCTIVE,
        }.get(action, ToolRisk.STATE_CHANGING)

    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_against_schema(arguments, FILESYSTEM_SCHEMA)
        args = dict(arguments)
        safe_resolve(args["path"], self._workspace_root)  # structural check early
        if args["action"] == "write" and "content" not in args:
            raise ValueError("write requires content")
        return args

    def classification(self, arguments: dict[str, Any]) -> str:
        return classify(arguments["path"], arguments.get("content"))

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        action = arguments["action"]
        target = safe_resolve(arguments["path"], self._workspace_root)

        if action == "read":
            return self._read(target)
        if action == "list":
            return self._list(target)
        if action == "write":
            return self._write(target, arguments.get("content", ""))
        if action == "delete":
            return self._delete(target)
        raise ValidationError(f"unknown action {action}")

    async def dry_run(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        target = safe_resolve(arguments["path"], self._workspace_root)
        action = arguments["action"]
        return ToolResult(
            success=True,
            data={
                "dry_run": True,
                "action": action,
                "path": str(target),
                "exists": target.exists(),
                "classification": classify(str(target), arguments.get("content")),
            },
            evidence={"dry_run": True},
        )

    # ---------------------------------------------------------------- actions

    def _read(self, target: Path) -> ToolResult:
        if not target.is_file():
            return ToolResult.failure("file does not exist", evidence={"exists": False})
        data = target.read_bytes()[:MAX_READ_BYTES]
        return ToolResult(
            success=True,
            data={"path": str(target), "content": data.decode("utf-8", errors="replace")},
            evidence={"exists": True, "size_bytes": target.stat().st_size},
        )

    def _list(self, target: Path) -> ToolResult:
        if not target.is_dir():
            return ToolResult.failure("directory does not exist", evidence={"exists": False})
        entries = []
        for p in sorted(target.iterdir())[:200]:
            entry: dict[str, Any] = {"name": p.name, "is_dir": p.is_dir()}
            if p.is_file():
                entry["size"] = p.stat().st_size
            entries.append(entry)
        return ToolResult(success=True, data={"path": str(target), "entries": entries})

    def _write(self, target: Path, content: str) -> ToolResult:
        existed = target.exists()
        hash_before = (
            hashlib.sha256(target.read_bytes()).hexdigest() if existed else None
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        hash_after = hashlib.sha256(target.read_bytes()).hexdigest()
        return ToolResult(
            success=True,
            data={"path": str(target), "bytes_written": len(content.encode("utf-8"))},
            evidence={
                "path": str(target),
                "exists": True,
                "hash_before": hash_before,
                "hash_after": hash_after,
            },
        )

    def _delete(self, target: Path) -> ToolResult:
        if not target.exists():
            return ToolResult.failure("file does not exist", evidence={"exists": False})
        hash_before = (
            hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        )
        if target.is_dir():
            raise ValidationError("directory deletion requires dedicated tooling (BP §197)")
        target.unlink()
        return ToolResult(
            success=True,
            data={"path": str(target), "deleted": True},
            evidence={
                "path": str(target),
                "exists": False,
                "hash_before": hash_before,
            },
        )


__all__ = ["FILESYSTEM_SCHEMA", "FilesystemTool", "safe_resolve"]
