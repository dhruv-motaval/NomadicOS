"""Process brick (SPEC §22, §26, Phase 12): list, launch and stop processes.

- ``list``: a bounded snapshot of running processes (pid + name).
- ``launch``: start an application DETACHED from the task. Unlike terminal
  commands, whose process trees are cleaned up with the task, a launched
  app is meant to keep running. It gets the same owner-surface screen and
  tool-process marker as the terminal brick, so it is no side door to the
  owner CLI or API (SPEC §4, §56.5). Console programs belong in the
  terminal brick, which captures their output.
- ``stop``: terminate one process by pid. NomadicOS itself, its ancestors
  (the shell or host that runs it), other users' processes and core Windows
  processes are refused.

Runs only on AuthorizedAction artifacts, like every tool; it makes no
permission or task-level decisions. Process names are untrusted DATA.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.desktop.contracts import MAX_PROCESSES
from nomadicos.kernel.errors import AuthorizationDenied, Failure
from nomadicos.tools.base import Tool, ToolOutcome, error_text
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import deny_protected, normalize_root, resolve_scoped
from nomadicos.tools.terminal import deny_owner_surfaces, tool_process_env

MAX_LAUNCH_ARGS = 32
MAX_ARG_CHARS = 1000
#: how long launch watches for an immediate crash before reporting
LAUNCH_SETTLE_S = 1.0
#: grace a stopped process gets before ``force`` kills it
STOP_GRACE_S = 5.0

#: core Windows processes: stopping one breaks the session or the machine
PROTECTED_PROCESS_NAMES = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "secure system",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "explorer.exe",
        "sihost.exe",
        "ctfmon.exe",
        "msmpeng.exe",
        "securityhealthservice.exe",
    }
)


class ProcessDenied(AuthorizationDenied):
    """A stop or launch that must never happen, whatever the grant."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessListArgs(_Args):
    #: case-insensitive substring of the process name
    name: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=50, ge=1, le=MAX_PROCESSES)


class ProcessLaunchArgs(_Args):
    #: a program name found on PATH or in App Paths ("notepad", "chrome"),
    #: or a path to an executable
    executable: str = Field(min_length=1, max_length=260)
    args: list[str] = Field(default_factory=list, max_length=MAX_LAUNCH_ARGS)
    cwd: str | None = Field(default=None, min_length=1, max_length=260)


class ProcessStopArgs(_Args):
    pid: int = Field(ge=1)
    #: kill if the process ignores the polite terminate request
    force: bool = False


def _app_path(name: str) -> str | None:
    """Windows "App Paths" registration (how Run/Start find chrome.exe)."""
    if os.name != "nt":
        return None
    import winreg

    exe = name if name.lower().endswith(".exe") else f"{name}.exe"
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        path = os.path.expandvars(str(value)).strip().strip('"')
        if path and Path(path).is_file():
            return path
    return None


def _protected_pids() -> set[int]:
    """NomadicOS and everything above it: stopping one ends the runtime or
    the owner's session around it."""
    me = psutil.Process()
    pids = {me.pid}
    try:
        pids.update(parent.pid for parent in me.parents())
    except psutil.Error:
        pass
    return pids


class ProcessTool(Tool):
    name = "process"
    ops: ClassVar[dict[str, type[BaseModel]]] = {
        "list": ProcessListArgs,
        "launch": ProcessLaunchArgs,
        "stop": ProcessStopArgs,
    }

    def __init__(self) -> None:
        #: launched apps, held so their handles are released cleanly
        self._launched: dict[int, subprocess.Popen[bytes]] = {}

    async def run(self, operation: str, args: dict[str, Any], ctx: ExecutionContext) -> ToolOutcome:
        model = self.ops.get(operation)
        if model is None:
            return _errored(f"process has no operation {operation!r}")
        try:
            typed = model.model_validate(args)
        except ValidationError as exc:
            return _errored(f"process.{operation} args: {error_text(exc)}")
        started = time.monotonic()
        if isinstance(typed, ProcessListArgs):
            outcome = self._list(typed)
        elif isinstance(typed, ProcessLaunchArgs):
            outcome = await self._launch(typed, ctx)
        else:
            assert isinstance(typed, ProcessStopArgs)
            outcome = await self._stop(typed)
        return outcome.model_copy(update={"duration_s": round(time.monotonic() - started, 3)})

    # ------------------------------------------------------------ list ---

    def _list(self, a: ProcessListArgs) -> ToolOutcome:
        needle = a.name.casefold() if a.name else None
        found: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name"]):
            name = str(proc.info.get("name") or "")
            if needle and needle not in name.casefold():
                continue
            found.append({"pid": int(proc.info["pid"]), "name": name[:100]})
            if len(found) >= a.limit:
                break
        lines = [f"{p['pid']} {p['name']}" for p in found]
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout="\n".join(lines) if lines else "(no matching process)",
            evidence={"count": len(found), "processes": found},
        )

    # ---------------------------------------------------------- launch ---

    def _resolve(self, raw: str, ctx: ExecutionContext) -> str:
        if Path(raw).is_absolute() or "/" in raw or "\\" in raw:
            resolved = resolve_scoped(raw, ctx)
            if not resolved.is_file():
                raise FileNotFoundError(f"no executable at {resolved}")
            return str(resolved)
        found = shutil.which(raw) or _app_path(raw)
        if found is None:
            raise FileNotFoundError(f"application not found: {raw}")
        deny_protected(normalize_root(found), ctx)
        return found

    async def _launch(self, a: ProcessLaunchArgs, ctx: ExecutionContext) -> ToolOutcome:
        for arg in a.args:
            if len(arg) > MAX_ARG_CHARS:
                return _errored(f"argument longer than {MAX_ARG_CHARS} characters")
        cwd = resolve_scoped(a.cwd, ctx) if a.cwd else ctx.workspace.resolve()
        deny_owner_surfaces((a.executable, *a.args), cwd, ctx)
        try:
            executable = self._resolve(a.executable, ctx)
        except FileNotFoundError as exc:
            return _failed(str(exc))
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                [executable, *a.args],
                cwd=str(cwd),
                env=tool_process_env(None),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
        except OSError as exc:
            return _failed(f"launch failed: {exc}")
        self._prune()
        self._launched[proc.pid] = proc
        deadline = time.monotonic() + LAUNCH_SETTLE_S
        while time.monotonic() < deadline and proc.poll() is None:
            await asyncio.sleep(0.1)
        exit_code = proc.poll()
        evidence: dict[str, Any] = {
            "pid": proc.pid,
            "executable": executable,
            "argv": [executable, *a.args][:MAX_LAUNCH_ARGS],
            "running": exit_code is None,
        }
        if exit_code is not None and exit_code != 0:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"{Path(executable).name} exited at once with code {exit_code}",
                exit_code=exit_code,
                process_id=proc.pid,
                evidence=evidence,
            )
        note = (
            f"{Path(executable).name} started (pid {proc.pid})"
            if exit_code is None
            # e.g. Windows 11 notepad.exe hands off to the packaged app
            else f"{Path(executable).name} exited at once with code 0; it may have "
            "handed off to another process, so check with desktop.list_windows or ui_tree"
        )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout=note,
            exit_code=exit_code,
            process_id=proc.pid,
            evidence=evidence,
        )

    def _prune(self) -> None:
        for pid in [pid for pid, proc in self._launched.items() if proc.poll() is not None]:
            del self._launched[pid]

    # ------------------------------------------------------------ stop ---

    async def _stop(self, a: ProcessStopArgs) -> ToolOutcome:
        try:
            proc = psutil.Process(a.pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            return _failed(f"no process with pid {a.pid}")
        except psutil.AccessDenied as exc:
            raise ProcessDenied(f"pid {a.pid} belongs to another user or the system") from exc
        if a.pid in _protected_pids():
            raise ProcessDenied(f"pid {a.pid} ({name}) runs NomadicOS or its host")
        if name.casefold() in PROTECTED_PROCESS_NAMES:
            raise ProcessDenied(f"{name} is a core Windows process")
        try:
            owner = proc.username()
            me = psutil.Process().username()
        except psutil.Error as exc:
            raise ProcessDenied(f"cannot confirm who owns pid {a.pid}") from exc
        if owner != me:
            raise ProcessDenied(f"pid {a.pid} ({name}) belongs to another user")
        proc.terminate()
        _gone, alive = await asyncio.to_thread(psutil.wait_procs, [proc], STOP_GRACE_S)
        forced = False
        if alive and a.force:
            proc.kill()
            forced = True
            _gone, alive = await asyncio.to_thread(psutil.wait_procs, [proc], STOP_GRACE_S)
        evidence = {"pid": a.pid, "name": name[:100], "stopped": not alive, "forced": forced}
        if alive:
            return ToolOutcome(
                status=ExecutionStatus.FAILED,
                failure=Failure.ACTION_FAILED,
                message=f"{name} (pid {a.pid}) is still running; retry with force=true",
                evidence=evidence,
            )
        return ToolOutcome(
            status=ExecutionStatus.SUCCEEDED,
            stdout=f"stopped {name} (pid {a.pid})",
            evidence=evidence,
        )


def _errored(message: str) -> ToolOutcome:
    return ToolOutcome(status=ExecutionStatus.ERRORED, failure=Failure.TOOL_ERROR, message=message)


def _failed(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ExecutionStatus.FAILED, failure=Failure.ACTION_FAILED, message=message
    )
