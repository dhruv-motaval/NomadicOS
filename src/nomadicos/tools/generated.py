"""Generated script tools — the growing toolbox (owner-vision: self-implementing).

When the agent solves a novel task by writing and running a script, the script
is persisted under ``data/scripts/`` with a small header and registered as a
first-class Tool on every boot. The toolbox therefore grows from experience —
entirely locally (I11), gated like every other tool (I5), and owner-inspectable
(I4: the owner can read or delete any file in data/scripts/).

Script contract (inside each .py, comment header):
    # nomadicos-tool
    # description: <one line>
    # arguments_schema: <JSON schema object, defaults to {}>
Execution contract: print a JSON object to stdout:
    {"summary": "<one line>", "data": {...optional...}}
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from nomadicos.core.errors import ToolExecutionError
from nomadicos.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
    validate_against_schema,
)

_HEADER_MARKER = "# nomadicos-tool"
_MAX_SCRIPT_LINES = 200


def parse_script_header(path: Path) -> dict[str, Any] | None:
    """Parse the nomadicos-tool header from a script file. None if invalid."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != _HEADER_MARKER:
        return None
    description = ""
    schema: dict[str, Any] = {}
    for line in lines[1 : 12]:
        stripped = line.strip()
        if stripped.startswith("# description:"):
            description = stripped[len("# description:"):].strip()
        elif stripped.startswith("# arguments_schema:"):
            try:
                schema = json.loads(stripped[len("# arguments_schema:"):].strip())
            except json.JSONDecodeError:
                schema = {}
    if not description:
        return None
    return {"description": description, "arguments_schema": schema}


class GeneratedScriptTool(Tool):
    """Executes one model-generated script under data/scripts/ via the same
    interpreter as the runtime. Every execution still passes the Security
    Gate (I5) — the script being self-written changes nothing about mediation."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._name = f"script.{path.stem.lower().replace('-', '_')}"
        header = parse_script_header(path)
        self._description = (header or {}).get("description", path.stem)
        self._schema = (header or {}).get("arguments_schema") or {}
        self._spec = ToolSpec(
            name=self._name,
            description=f"(generated) {self._description}",
            risk=ToolRisk.STATE_CHANGING,
            arguments_schema=self._schema,
            side_effects=["runs a locally generated script", "may modify files"],
            evidence_kind="terminal",
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def source_path(self) -> Path:
        return self._path

    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_against_schema(arguments, self._spec.arguments_schema)
        return dict(arguments)

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        # refresh the header: the owner may have edited the script (I4)
        header = parse_script_header(self._path)
        if header is None:
            raise ToolExecutionError(
                f"generated script {self._path.name} is missing a valid header"
            )
        args_json = json.dumps(arguments)
        import time as _t

        started = _t.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._path),
                args_json,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._path.parent),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=120
            )
        except TimeoutError as exc:
            raise ToolExecutionError("generated script timed out after 120s") from exc
        latency = (_t.monotonic() - started) * 1000
        stdout_text = stdout.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise ToolExecutionError(
                f"generated script failed (exit {process.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[:300]}"
            )
        try:
            payload = json.loads(stdout_text.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise ToolExecutionError(
                "generated script did not print a valid JSON result"
            ) from exc
        return ToolResult(
            success=True,
            data={
                "summary": str(payload.get("summary", "script finished")),
                "result": payload.get("data", payload),
                "latency_ms": round(latency, 1),
                "stderr": stderr.decode("utf-8", errors="replace")[:300],
            },
        )


def load_generated_tools(scripts_dir: Path) -> list[GeneratedScriptTool]:
    """Register every valid script in data/scripts/ as a tool (boot step)."""
    tools: list[GeneratedScriptTool] = []
    if not scripts_dir.exists():
        return tools
    for path in sorted(scripts_dir.glob("*.py")):
        if parse_script_header(path) is not None:
            tools.append(GeneratedScriptTool(path))
    return tools


def save_generated_script(scripts_dir: Path, slug: str, content: str) -> Path | None:
    """Persist a model-written script if it carries a valid header. Best effort."""
    lines = content.strip().splitlines()
    if len(lines) > _MAX_SCRIPT_LINES or not lines:
        return None
    if lines[0].strip() != _HEADER_MARKER:
        lines.insert(0, _HEADER_MARKER)
    slug = slug.lower().replace(" ", "-")
    slug = "".join(ch for ch in slug if ch.isalnum() or ch == "-")[:60] or "tool"
    path = scripts_dir / f"{slug}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


__all__ = [
    "GeneratedScriptTool",
    "load_generated_tools",
    "parse_script_header",
    "save_generated_script",
]
