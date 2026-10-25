"""Terminal brick (SPEC §24-25): real OS processes, never a simulator.

The command surface is executable + argv only. No shell string is ever built
(no ``shell=True``, no command-line concatenation), so what the model writes
stays inert data: an argument containing ``> file`` is a literal argument, not
a redirection.

Windows programs that are not executables - cmd.exe builtins such as ``dir``
and ``.cmd``/``.bat`` scripts such as ``npm`` - are handed to the command
interpreter as separate argv elements (still nothing to parse), and shell
metacharacters are refused on that single route so chaining stays impossible.

The brick keeps a deliberately narrow claim surface:

- stdout and stderr are captured separately and stay distinguishable;
- a timeout, a cancellation, or task cleanup kills the OWNED process tree and
  nothing else - no orphans, never a system-wide sweep (SPEC §6.7);
- streams are drained to EOF so a writer cannot deadlock on a full pipe, while
  only a bounded prefix is kept as evidence;
- SUCCEEDED means only "the process ran and exited 0". Task success is not a
  terminal fact (SPEC §28, §6.11).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import psutil
from pydantic import BaseModel, Field, ValidationError, field_validator

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure
from nomadicos.tools.base import Tool, ToolOutcome, error_text
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import resolve_scoped

#: default per-command wall clock before the tree is killed
DEFAULT_TIMEOUT_S = 120.0
#: decoded-character budget kept per stream as evidence
MAX_STREAM_CHARS = 60_000
#: pipe read chunk
READ_CHUNK = 65_536
#: grace a terminated tree gets to die before it is killed outright
KILL_GRACE_S = 5.0

#: Windows script suffixes: files the OS cannot spawn directly
_SCRIPT_SUFFIXES = frozenset({".cmd", ".bat"})
#: cmd.exe builtins with no executable on disk (Windows only)
_CMD_BUILTINS = frozenset(
    {
        "assoc",
        "call",
        "cd",
        "chdir",
        "cls",
        "color",
        "copy",
        "date",
        "del",
        "dir",
        "echo",
        "endlocal",
        "erase",
        "exit",
        "for",
        "ftype",
        "goto",
        "if",
        "md",
        "mkdir",
        "mklink",
        "move",
        "path",
        "pause",
        "popd",
        "prompt",
        "pushd",
        "rd",
        "rem",
        "ren",
        "rename",
        "rmdir",
        "set",
        "setlocal",
        "shift",
        "start",
        "time",
        "title",
        "type",
        "ver",
        "vol",
    }
)
#: characters the command interpreter would re-interpret; refused on that route
_SHELL_META = frozenset('&|<>^"%\n\r\t')


class ExecuteArgs(BaseModel):
    """Typed ``terminal.execute`` arguments (SPEC §24)."""

    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    stdin: str | None = None

    @field_validator("command", mode="before")
    @classmethod
    def _strip_command(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("command", "cwd", "stdin", mode="after")
    @classmethod
    def _no_nulls(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("null bytes are not allowed")
        return value

    @field_validator("args", mode="before")
    @classmethod
    def _coerce_args(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("args")
    @classmethod
    def _no_null_args(cls, value: list[str]) -> list[str]:
        for item in value:
            if "\x00" in item:
                raise ValueError("null bytes are not allowed in arguments")
        return value

    @field_validator("env")
    @classmethod
    def _valid_env(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        for key, item in value.items():
            if not key or "=" in key or "\x00" in key or "\x00" in item:
                raise ValueError(f"invalid environment entry {key!r}")
        return value


class CommandProblem(Exception):
    """A requested command cannot be started as a real process."""


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """Resolved spawn plan: exactly what the OS will receive."""

    argv: tuple[str, ...]
    #: the spawned program itself (``cmd.exe`` when a route is interpreter-run)
    executable: str
    #: True when an interpreter fronts the command; metacharacters are refused
    via_interpreter: bool = False


def _tokenize(command: str) -> list[str]:
    """Split a command string into argv tokens using platform quoting rules.

    POSIX: shell-style quoting (double/single quotes, backslash escapes).
    Windows: whitespace separates tokens and double quotes group a token;
    nothing else is interpreted - backslashes, ``%``, ``&`` and ``>`` stay
    literal text, so paths and operands never change shape.
    """
    if os.name != "nt":
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()
    return _tokenize_windows(command)


def _tokenize_windows(command: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    open_token = False
    for char in command:
        if char == '"':
            quoted = not quoted
            open_token = True
        elif not quoted and char.isspace():
            if open_token:
                tokens.append("".join(current))
                current = []
                open_token = False
        else:
            current.append(char)
            open_token = True
    if open_token:
        tokens.append("".join(current))
    return tokens


def _pathext() -> list[str]:
    raw = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return [ext for ext in raw.split(os.pathsep) if ext]


def _find_executable(name: str) -> str | None:
    """Resolve the program to spawn.

    PATH search first; for an explicit path on Windows a missing PATHEXT
    extension is completed (``C:\\bin\\tool`` -> ``tool.exe``) because
    CreateProcess does not do it. No executable ever means no process.
    """
    found = shutil.which(name)
    if found or os.name != "nt" or not os.path.dirname(name):
        return found
    for ext in _pathext():
        found = shutil.which(name + ext)
        if found:
            return found
    return None


def _interpreter() -> str:
    found = _find_executable("cmd") if os.name == "nt" else None
    if not found:
        raise CommandProblem("process creation failed: command interpreter (cmd.exe) not found")
    return found


def _reject_shell_metacharacters(tokens: list[str]) -> None:
    """A route through the command interpreter re-parses its arguments, so
    chaining characters are refused there instead of being honored."""
    for token in tokens:
        bad = "".join(sorted({char for char in token if char in _SHELL_META}))
        if bad:
            raise CommandProblem(
                f"shell metacharacters {bad!r} are not allowed through the "
                f"command interpreter (token {token!r})"
            )


def resolve_command(command: str, args: list[str]) -> CommandPlan:
    """Turn model arguments into a spawn plan.

    Deterministic: the same tokens always resolve the same way, and a token is
    never re-parsed as shell text except on the interpreter route, where
    metacharacters are rejected outright.
    """
    tokens = _tokenize(command)
    if not tokens:
        raise CommandProblem("empty command")
    argv = [*tokens, *args]
    found = _find_executable(tokens[0])
    if found is None:
        if os.name == "nt" and tokens[0].lower() in _CMD_BUILTINS:
            interpreter = _interpreter()
            _reject_shell_metacharacters(argv)
            return CommandPlan((interpreter, "/d", "/c", *argv), interpreter, True)
        raise CommandProblem(f"process creation failed: executable not found: {tokens[0]}")
    if os.name == "nt" and Path(found).suffix.lower() in _SCRIPT_SUFFIXES:
        interpreter = _interpreter()
        _reject_shell_metacharacters(argv)
        return CommandPlan((interpreter, "/d", "/c", *argv), interpreter, True)
    return CommandPlan(tuple(argv), found)


#: Windows: the child gets its own process group, so console signals aimed at
#: this application cannot reach it (and the reverse). Zero elsewhere.
_NEW_PROCESS_GROUP = (
    int(subprocess.CREATE_NEW_PROCESS_GROUP)
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
    else 0
)


def _terminate(process: psutil.Process) -> None:
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:  # not ours to stop; exit stays honest
        pass


def _force_kill(process: psutil.Process) -> None:
    try:
        process.kill()
    except psutil.NoSuchProcess:
        pass
    except psutil.AccessDenied:  # pragma: no cover - platform dependent
        pass


def _running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:  # pragma: no cover - platform dependent
        return False


def kill_tree(pid: int, grace_s: float = KILL_GRACE_S) -> None:
    """Stop ``pid`` and every descendant - and nothing outside that tree.

    Terminate first, then kill whatever survives the grace window.
    """
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    except psutil.Error:  # pragma: no cover - platform dependent
        children = []
    tree = [*children, process]
    for member in tree:
        _terminate(member)
    for member in psutil.wait_procs(
        [member for member in tree if _running(member)], timeout=grace_s
    )[1]:
        _force_kill(member)


class ProcessSupervisor:
    """Per-task ledger of the processes THIS application started (SPEC §6.7).

    Cleanup only ever walks a task's own ledger: never a system-wide sweep and
    never across a task boundary. A PID is kept only while its command runs,
    so a finished command cannot be caught up in a later sweep.
    """

    def __init__(self) -> None:
        self._owned: dict[str, set[int]] = {}

    def register(self, task_id: str, pid: int) -> None:
        self._owned.setdefault(task_id, set()).add(pid)

    def release(self, task_id: str, pid: int) -> None:
        owned = self._owned.get(task_id)
        if owned is not None:
            owned.discard(pid)
            if not owned:
                self._owned.pop(task_id, None)

    def live_pids(self, task_id: str) -> list[int]:
        return sorted(pid for pid in self._owned.get(task_id, ()) if psutil.pid_exists(pid))

    def kill_owned(self, task_id: str) -> list[int]:
        """Terminal cleanup for one task: only processes that task started."""
        killed: list[int] = []
        for pid in sorted(self._owned.pop(task_id, ())):
            if pid <= 0 or pid == os.getpid():
                continue
            kill_tree(pid)
            killed.append(pid)
        return killed

    def kill_all(self) -> list[int]:
        """Application shutdown: the whole per-app ledger, one task at a time."""
        return [pid for task_id in list(self._owned) for pid in self.kill_owned(task_id)]


async def _capture(stream: asyncio.StreamReader | None, char_limit: int) -> tuple[str, int]:
    """Drain one pipe to EOF while keeping a bounded prefix as evidence.

    Reading to EOF is what prevents a writer deadlocking on a full pipe; the
    byte ceiling is what keeps memory flat for a loud process.
    """
    if stream is None:
        return "", 0
    byte_ceiling = char_limit * 4  # utf-8 worst case, so the char cap holds
    kept: list[bytes] = []
    kept_bytes = 0
    total_bytes = 0
    while True:
        chunk = await stream.read(READ_CHUNK)
        if not chunk:
            break
        total_bytes += len(chunk)
        if kept_bytes < byte_ceiling:
            kept.append(chunk)
            kept_bytes += len(chunk)
    text = b"".join(kept).decode("utf-8", errors="replace")
    if total_bytes > kept_bytes:
        hidden = total_bytes - kept_bytes
        return (
            text[:char_limit] + f"\n[truncated: {hidden} of {total_bytes} bytes not shown]",
            total_bytes,
        )
    if len(text) > char_limit:
        return text[:char_limit] + "\n[truncated]", total_bytes
    return text, total_bytes


async def _deliver_stdin(pipe: asyncio.StreamWriter, data: bytes) -> None:
    try:
        pipe.write(data)
        await pipe.drain()
    except OSError:
        pass  # the child exited, or never reads stdin
    finally:
        pipe.close()


class TerminalTool(Tool):
    """``terminal.execute`` - one foreground command as a real OS process."""

    name = "terminal"
    ops: ClassVar[dict[str, type[BaseModel]]] = {"execute": ExecuteArgs}

    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        self.supervisor = supervisor or ProcessSupervisor()

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        started = time.monotonic()
        if operation != "execute":
            return _errored(f"terminal has no operation {operation!r}", started)
        try:
            spec = ExecuteArgs.model_validate(args)
        except ValidationError as exc:
            return _errored(f"terminal.execute args: {error_text(exc)}", started)
        # cwd: default is the task workspace itself; an explicit path must stay
        # inside the scope this task is attributed to (SPEC §23). An escape is
        # an authorization refusal, not a tool failure, so it propagates.
        cwd = resolve_scoped(spec.cwd, ctx) if spec.cwd is not None else ctx.workspace.resolve()
        if not cwd.is_dir():
            return _failed(f"cwd does not exist: {cwd}", started)
        try:
            plan = resolve_command(spec.command, spec.args)
        except CommandProblem as exc:
            return _errored(str(exc), started, evidence={"cwd": str(cwd)})
        timeout = spec.timeout_s if spec.timeout_s is not None else DEFAULT_TIMEOUT_S
        return await self._execute(spec, plan, cwd, timeout, started, ctx)

    async def _execute(
        self,
        spec: ExecuteArgs,
        plan: CommandPlan,
        cwd: Path,
        timeout: float,
        started: float,
        ctx: ExecutionContext,
    ) -> ToolOutcome:
        try:
            process = await asyncio.create_subprocess_exec(
                *plan.argv,
                cwd=str(cwd),
                env=_merge_env(spec.env),
                stdin=asyncio.subprocess.PIPE
                if spec.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NEW_PROCESS_GROUP,
            )
        except (OSError, ValueError) as exc:
            return _errored(f"process creation failed: {type(exc).__name__}: {exc}", started)

        self.supervisor.register(ctx.task_id, process.pid)
        timed_out = False
        try:
            if spec.stdin is not None and process.stdin is not None:
                await _deliver_stdin(process.stdin, spec.stdin.encode())
            out_task = asyncio.create_task(_capture(process.stdout, MAX_STREAM_CHARS))
            err_task = asyncio.create_task(_capture(process.stderr, MAX_STREAM_CHARS))
            wait_task = asyncio.create_task(process.wait())
            pending: set[asyncio.Task[Any]] = (
                await asyncio.wait({out_task, err_task, wait_task}, timeout=timeout)
            )[1]
            if pending:
                # Timeout: the tree dies, which closes the pipes and finishes
                # the reads and the wait.
                timed_out = True
                kill_tree(process.pid)
                pending = (await asyncio.wait(pending, timeout=KILL_GRACE_S))[1]
        except asyncio.CancelledError:
            # Cancellation must not leave a running process behind either.
            kill_tree(process.pid)
            raise
        finally:
            self.supervisor.release(ctx.task_id, process.pid)

        for stray in pending:
            stray.cancel()
        stdout, stdout_bytes = (
            out_task.result() if out_task.done() and not out_task.cancelled() else ("", 0)
        )
        stderr, stderr_bytes = (
            err_task.result() if err_task.done() and not err_task.cancelled() else ("", 0)
        )
        exit_code = process.returncode
        duration = round(time.monotonic() - started, 3)
        evidence = {
            "command": spec.command,
            "args": list(spec.args),
            "argv": list(plan.argv),
            "executable": plan.executable,
            "via_interpreter": plan.via_interpreter,
            "cwd": str(cwd),
            "killed_by_timeout": timed_out,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
        }
        if timed_out:
            return ToolOutcome(
                status=ExecutionStatus.TIMED_OUT,
                stdout=stdout,
                stderr=stderr,
                failure=Failure.TIMEOUT,
                process_id=process.pid,
                message=f"timeout after {timeout}s",
                duration_s=duration,
                evidence=evidence,
            )
        if exit_code is None:
            return _errored("process ended without reporting an exit code", started)
        succeeded = exit_code == 0
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED if succeeded else ExecutionStatus.FAILED,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            failure=None if succeeded else Failure.ACTION_FAILED,
            process_id=process.pid,
            message="" if succeeded else f"exit code {exit_code}",
            duration_s=duration,
            evidence=evidence,
        )


def _merge_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Inherited environment plus the requested entries (SPEC §24)."""
    if not extra:
        return dict(os.environ)
    return {**os.environ, **extra}


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 3)


def _errored(message: str, started: float, evidence: dict[str, Any] | None = None) -> ToolOutcome:
    return ToolOutcome(
        status=ExecutionStatus.ERRORED,
        failure=Failure.TOOL_ERROR,
        message=message,
        duration_s=_elapsed(started),
        evidence=evidence or {},
    )


def _failed(message: str, started: float, evidence: dict[str, Any] | None = None) -> ToolOutcome:
    return ToolOutcome(
        status=ExecutionStatus.FAILED,
        failure=Failure.ACTION_FAILED,
        message=message,
        duration_s=_elapsed(started),
        evidence=evidence or {},
    )
