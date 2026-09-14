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
from pathlib import Path
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
from nomadicos.tools.filesystem import safe_resolve

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
    "format",
    "shutdown",
    "taskkill",
    "reg",
    "bcdedit",
    "vssadmin",
    "cipher",
}

# cmd.exe builtins: not executables, so create_subprocess_exec can't find them.
# They are routed through `cmd /c` with each token kept as a separate argv
# element (still no shell string concatenation — BP §194 holds), and shell
# metacharacters inside tokens are rejected outright (no command chaining).
CMD_BUILTINS = {
    "start",
    "dir",
    "type",
    "mkdir",
    "del",
    "copy",
    "move",
    "echo",
    "rmdir",
    "ren",
    "cls",
    "md",
    "rd",
    "title",
    "ver",
    "vol",
}
_SHELL_META = re.compile(r"[&|<>^%]")
_TRAVERSAL = re.compile(r"\.\.([\\/]|$)|(^|[\\/])\.\.$")
# drive-letter absolute path with a boundary (so "http://…" is not matched)
_WIN_ABS = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"']*")
_POSIX_ABS = re.compile(r"(?<!\S)/\S+")


def _reject_traversal(tok: str) -> None:
    if _TRAVERSAL.search(str(tok)):
        raise ValidationError(
            "parent-directory ('..') path arguments are refused (workspace confinement)",
            context={"reason": "filesystem confinement parity (BP §92)", "code": "PATH_TRAVERSAL"},
        )


def _reject_escaped_abs(tok: str, workspace_root: str | None) -> None:
    """Absolute paths embedded in arguments must stay inside the workspace —
    6A.5 caught `python -c "shutil.copy('../x','stolen')` style smuggling where
    '..' was not adjacent to a separator boundary; the absolute-path arm of the
    same confinement contract closes the direct-absolute variant."""
    if not workspace_root:
        return
    root = Path(workspace_root).resolve()
    candidates = _WIN_ABS.findall(str(tok))
    if os.name != "nt":  # on Windows a leading '/' token is a cmd switch, not a path
        candidates += _POSIX_ABS.findall(str(tok))
    for raw in candidates:
        try:
            p = Path(raw).resolve()
        except OSError:
            continue
        if p.is_absolute() and not p.is_relative_to(root) and root not in p.parents and p != root:
            raise ValidationError(
                f"absolute path outside the task workspace is refused: {raw}",
                context={"reason": "workspace confinement (BP §92)", "code": "PATH_ESCAPE"},
            )


def build_command(arguments: dict[str, Any]) -> list[str]:
    """Structured command array (BP §194). No shell string concatenation."""
    command = str(arguments["command"]).strip()
    if not command:
        raise ValidationError("command must be non-empty", context={"code": "INVALID_COMMAND"})
    tokens = command.split()
    first = tokens[0].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first in BLOCKED_COMMAND_NAMES:
        raise ValidationError(
            f"command is blocked by policy: {first}",
            context={
                "reason": "administrative system command",
                "code": "POLICY_BLOCKED_COMMAND",
            },
        )
    args = [str(a) for a in arguments.get("args", [])]
    # BP §92 parity with the filesystem tool: no command may address a path
    # outside its (confined) working directory via parent-directory traversal.
    for tok in [*tokens, *args]:
        _reject_traversal(tok)
    if first in CMD_BUILTINS:
        for token in [*tokens, *args]:
            if _SHELL_META.search(token):
                raise ValidationError(
                    f"shell metacharacters are not allowed in builtins: {token}",
                    context={
                        "reason": "command chaining is blocked (BP §194)",
                        "code": "SHELL_METACHAR_REJECTED",
                    },
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
        for tok in [str(args.get("command", "")), *[str(a) for a in args.get("args", [])]]:
            _reject_escaped_abs(tok, self._workspace_root or args.get("working_dir"))
        if args.get("working_dir"):
            # STEP 6A contract parity: working_dir resolves against the task
            # workspace and cannot escape it — same rule as filesystem paths.
            args["working_dir"] = str(safe_resolve(args["working_dir"], self._workspace_root))
        timeout = args.get("timeout_seconds", DEFAULT_TIMEOUT)
        if not (0 < float(timeout) <= 600):
            raise ValueError("timeout_seconds must be within (0, 600]")
        return args

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        argv = build_command(arguments)
        working_dir = arguments.get("working_dir") or self._workspace_root or "."
        timeout = float(arguments.get("timeout_seconds", DEFAULT_TIMEOUT))

        # minimal environment (BP §162)
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }

        started = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        popen_kwargs: dict = {}
        if os.name == "nt":
            import subprocess as _sp

            popen_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW  # no GUI dialogs from start
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **popen_kwargs,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # STEP 5 & STEP 6A: a timed-out or cancelled command MUST NOT survive
            # as an orphan shell — kill and reap the child process cleanly.
            with_context = {"timeout_seconds": timeout}
            if process is not None:
                try:
                    if process.returncode is None:
                        process.kill()
                        with_context["killed"] = True
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:  # noqa: BLE001 — reaping best effort
                    with_context["killed"] = False
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ValidationError(
                f"command timed out after {timeout}s",
                context={**with_context, "code": "COMMAND_TIMEOUT"},
            ) from exc
        except FileNotFoundError as exc:
            raise ValidationError(
                f"command not found: {argv[0]}",
                context={"command": argv[0], "code": "COMMAND_NOT_FOUND"},
            ) from exc

        latency_ms = (time.monotonic() - started) * 1000
        stdout_text = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
        stderr_text = stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
        data = {
            "exit_code": process.returncode,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "latency_ms": round(latency_ms, 1),
        }
        evidence = {
            "exit_code": process.returncode,
            "stdout": stdout_text[:512],
            "stderr": stderr_text[:512],
            "duration_seconds": round(latency_ms / 1000, 2),
        }
        if process.returncode != 0:
            err_msg = (
                f"command failed (exit {process.returncode}): "
                f"{(stderr_text or stdout_text).strip()[:300]}"
            )
            return ToolResult(
                success=False,
                data=data,
                error=err_msg,
                error_code="COMMAND_NONZERO_EXIT",
                evidence=evidence,
            )
        return ToolResult(
            success=True,
            data=data,
            evidence=evidence,
        )


__all__ = ["TERMINAL_SCHEMA", "TerminalTool", "build_command"]
