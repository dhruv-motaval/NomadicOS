"""Terminal/process tool (BP §13, §90, §194; ADR-0012/0018).

Security properties:
- structured argument arrays, never shell string concatenation (BP §194)
- output size-bounded (BP §87)
- environment minimization (BP §162)
- evidence: exit code + stdout/stderr + duration (BP §143)
"""

import asyncio
import os
import re
import time
from typing import Any

from nomadicos.core.errors import ValidationError
from nomadicos.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
    validate_against_schema,
)

TERMINAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "minLength": 1, "maxLength": 4096},
        "args": {"type": "array", "maxItems": 64},
        "working_dir": {"type": "string", "maxLength": 1024},
        "timeout_seconds": {"type": "number"},
    },
    "required": ["command"],
    "additionalProperties": False,
}

MAX_OUTPUT_BYTES = 128 * 1024
DEFAULT_TIMEOUT = 30.0
BLOCKED_COMMAND_NAMES = {
    # never allow raw system-altering commands without dedicated tools (BP §82)
    "format", "shutdown", "taskkill", "reg", "bcdedit", "vssadmin", "cipher",
}

# cmd.exe builtins: not executables, so create_subprocess_exec can't find them.
# They are routed through `cmd /c` with each token kept as a separate argv
# element (still no shell string concatenation — BP §194 holds), and shell
# metacharacters inside tokens are rejected outright (no command chaining).
CMD_BUILTINS = {
    "start", "dir", "type", "mkdir", "del", "copy", "move", "echo",
    "rmdir", "ren", "cls", "md", "rd", "title", "ver", "vol",
}
_SHELL_META = re.compile(r"[&|<>^%]")


def build_command(arguments: dict[str, Any]) -> list[str]:
    """Structured command array (BP §194). No shell string concatenation."""
    command = str(arguments["command"]).strip()
    if not command:
        raise ValidationError("command must be non-empty")
    tokens = command.split()
    first = tokens[0].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first in BLOCKED_COMMAND_NAMES:
        raise ValidationError(
            f"command is blocked by policy: {first}",
            context={"reason": "administrative system command"},
        )
    args = [str(a) for a in arguments.get("args", [])]
    if first in CMD_BUILTINS:
        for token in [*tokens, *args]:
            if _SHELL_META.search(token):
                raise ValidationError(
                    f"shell metacharacters are not allowed in builtins: {token}",
                    context={"reason": "command chaining is blocked (BP §194)"},
                )
        return ["cmd", "/c", *tokens, *args]
    return [*tokens, *args]


class TerminalTool(Tool):
    """STATE_CHANGING tool. Destructive/ADMINISTRATIVE commands are blocked at
    schema level; the Security Gate decides per policy."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self._workspace_root = workspace_root

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="terminal",
            description="execute a structured command (no shell) and capture output",
            risk=ToolRisk.STATE_CHANGING,
            arguments_schema=TERMINAL_SCHEMA,
            side_effects=["spawns a child process", "may modify files"],
            supports_dry_run=False,
        )

    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_against_schema(arguments, TERMINAL_SCHEMA)
        args = dict(arguments)
        build_command(args)  # structural check early (BP §194)
        timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT)
        if not (0 < float(timeout) <= 600):
            raise ValueError("timeout_seconds must be within (0, 600]")
        return args

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        argv = build_command(arguments)
        working_dir = arguments.get("working_dir") or self._workspace_root or "."
        timeout = float(arguments.get("timeout_seconds", DEFAULT_TIMEOUT))

        # minimal environment (BP §162)
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            raise ValidationError(
                f"command timed out after {timeout}s",
                context={"timeout_seconds": timeout},
            ) from exc
        except FileNotFoundError as exc:
            raise ValidationError(
                f"command not found: {argv[0]}", context={"command": argv[0]}
            ) from exc

        latency_ms = (time.monotonic() - started) * 1000
        stdout_text = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
        stderr_text = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
        return ToolResult(
            success=process.returncode == 0,
            data={
                "exit_code": process.returncode,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "latency_ms": round(latency_ms, 1),
            },
            evidence={
                "exit_code": process.returncode,
                "stdout": stdout_text[:512],
                "duration_seconds": round(latency_ms / 1000, 2),
            },
        )


__all__ = ["TERMINAL_SCHEMA", "TerminalTool", "build_command"]
