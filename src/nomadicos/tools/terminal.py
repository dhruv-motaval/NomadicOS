"""Terminal brick (SPEC §24-25): real OS processes, never a simulator.

Deterministic executable+argv. No shell-string concatenation; stdout/stderr
stay distinguishable; timeout kills the whole owned process tree; exit_code
0 + completed = success, nothing more is claimed here (§6.11).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, ClassVar

import psutil
from pydantic import BaseModel, Field

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure
from nomadicos.tools.base import Tool, ToolOutcome
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import resolve_scoped

MAX_STREAM_CHARS = 60_000


def _clean_env(env: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in env.items():
        if not key or "=" in key:
            raise ValueError(f"invalid environment key {key!r}")
        out[key] = value
    return out


class ExecuteArgs(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout_s: float | None = Field(default=None, gt=0)
    stdin: str | None = None


class ProcessSupervisor:
    """Tracks live child processes per task; cleanup never crosses tasks."""

    def __init__(self) -> None:
        self._owned: dict[str, set[int]] = {}

    def register(self, task_id: str, pid: int) -> None:
        self._owned.setdefault(task_id, set()).add(pid)

    def release(self, task_id: str, pid: int) -> None:
        self._owned.get(task_id, set()).discard(pid)

    def live_pids(self, task_id: str) -> set[int]:
        return {p for p in self._owned.get(task_id, set()) if psutil.pid_exists(p)}

    def kill_owned(self, task_id: str) -> list[int]:
        """Terminate only processes this task started."""
        killed: list[int] = []
        for pid in list(self._owned.get(task_id, set())):
            try:
                proc = psutil.Process(pid)
            except psutil.NoSuchProcess:
                continue
            _kill_tree(proc)
            killed.append(pid)
        self._owned.pop(task_id, None)
        return killed

    def kill_all(self) -> list[int]:
        """Shutdown helper: only ever touches processes THIS supervisor (and
        therefore this app instance) started — a per-app ledger, not a
        system-wide sweep (SPEC §6.7)."""
        killed: list[int] = []
        for task_id in list(self._owned):
            killed.extend(self.kill_owned(task_id))
        return killed


def _kill_tree(proc: psutil.Process) -> None:
    children: list[psutil.Process] = []
    try:
        children = proc.children(recursive=True)
    except psutil.Error:  # pragma: no cover
        pass
    for child in children:
        try:
            child.terminate()
        except psutil.Error:  # pragma: no cover
            pass
    try:
        proc.terminate()
    except psutil.Error:  # pragma: no cover
        pass
    gone, alive = psutil.wait_procs([*children, proc], timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:  # pragma: no cover
            pass


class TerminalTool(Tool):
    name = "terminal"
    ops: ClassVar[dict[str, type[BaseModel]]] = {"execute": ExecuteArgs}

    def __init__(self, supervisor: ProcessSupervisor | None = None) -> None:
        self.supervisor = supervisor or ProcessSupervisor()

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        assert operation == "execute"
        a = ExecuteArgs.model_validate(args)
        if not a.command.strip():
            return ToolOutcome(
                status=ExecutionStatus.ERRORED, failure=Failure.TOOL_ERROR, message="empty command"
            )
        cwd = resolve_scoped(a.cwd or str(ctx.workspace), ctx)
        if not cwd.is_dir():
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"cwd does not exist: {cwd}",
            )
        try:
            extra = _clean_env(a.env or {})
        except ValueError as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED, failure=Failure.TOOL_ERROR, message=str(exc)
            )
        env = {**os.environ, **extra}
        started = time.monotonic()
        timeout = a.timeout_s if a.timeout_s is not None else 120.0
        try:
            proc = await asyncio.create_subprocess_exec(
                a.command,
                *a.args,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE
                if a.stdin is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            return ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"process creation failed: {type(exc).__name__}: {exc}",
                evidence={"cwd": str(cwd)},
            )
        self.supervisor.register(ctx.task_id, proc.pid)
        timed_out = False
        if a.stdin is not None and proc.stdin is not None:
            try:
                proc.stdin.write(a.stdin.encode())
                await proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError, ValueError):  # pragma: no cover
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # pragma: no cover
                    pass

        async def _read(stream: asyncio.StreamReader | None) -> bytes:
            return await stream.read() if stream else b""

        out_task = asyncio.create_task(_read(proc.stdout))
        err_task = asyncio.create_task(_read(proc.stderr))
        wait_task = asyncio.create_task(proc.wait())
        timed_out = False
        try:
            done, pending = await asyncio.wait({out_task, err_task, wait_task}, timeout=timeout)
            if pending:  # timeout: kill the tree, then the pipes hit EOF
                timed_out = True
                try:
                    _kill_tree(psutil.Process(proc.pid))
                except psutil.NoSuchProcess:
                    pass
                done, pending = await asyncio.wait({out_task, err_task, wait_task}, timeout=5)
            for stray in pending:  # pragma: no cover
                stray.cancel()
        except TimeoutError:  # pragma: no cover
            timed_out = True
        finally:
            self.supervisor.release(ctx.task_id, proc.pid)
        out_b = out_task.result() if out_task.done() and not out_task.cancelled() else b""
        err_b = err_task.result() if err_task.done() and not err_task.cancelled() else b""
        code = proc.returncode if not wait_task.cancelled() else None
        duration = time.monotonic() - started
        stdout = _decode(out_b)
        stderr = _decode(err_b)
        evidence = {
            "command": a.command,
            "args": a.args,
            "cwd": str(cwd),
            "killed_by_timeout": timed_out,
        }
        if timed_out:
            return ToolOutcome(
                status=ExecutionStatus.TIMED_OUT,
                stdout=stdout,
                stderr=stderr,
                failure=Failure.TIMEOUT,
                process_id=proc.pid,
                message=f"timeout after {timeout}s",
                duration_s=round(duration, 3),
                evidence=evidence,
            )
        if code is None:
            code = proc.returncode
        status = ExecutionStatus.SUCCEEDED if code == 0 else ExecutionStatus.FAILED
        return ToolOutcome(
            status=status,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            failure=None if code == 0 else Failure.ACTION_FAILED,
            process_id=proc.pid,
            message="" if code == 0 else f"exit code {code}",
            duration_s=round(duration, 3),
            evidence=evidence,
        )


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > MAX_STREAM_CHARS:
        return text[:MAX_STREAM_CHARS] + f"\n…[truncated {len(text)} chars]"
    return text
