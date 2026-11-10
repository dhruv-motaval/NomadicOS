"""Process brick (SPEC §22, §26, Phase 12): real processes, honest results.

Launch starts real OS processes (this interpreter, so nothing depends on
what is installed); stop refuses NomadicOS itself, its host and core Windows
processes; a launch can never reach an owner surface.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psutil
import pytest

from nomadicos.action_ir.validation import _resource_hint
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.tools import ExecutionContext
from nomadicos.tools.paths import PathDenied
from nomadicos.tools.process import ProcessDenied, ProcessTool
from nomadicos.tools.terminal import TOOL_PROCESS_ENV

PY = sys.executable


def ctx(tmp_path: Path) -> ExecutionContext:
    return ExecutionContext.for_task("task_proc", tmp_path / "ws", full_pc=True)


async def test_list_finds_this_interpreter(tmp_path: Path) -> None:
    out = await ProcessTool().run("list", {"name": "python"}, ctx(tmp_path))
    assert out.status is ExecutionStatus.SUCCEEDED
    assert os.getpid() in {p["pid"] for p in out.evidence["processes"]}


async def test_launch_then_stop_a_real_process(tmp_path: Path) -> None:
    tool = ProcessTool()
    out = await tool.run(
        "launch", {"executable": PY, "args": ["-c", "import time; time.sleep(60)"]}, ctx(tmp_path)
    )
    assert out.status is ExecutionStatus.SUCCEEDED, out.message
    pid = out.process_id
    assert pid is not None and out.evidence["running"] is True
    assert psutil.pid_exists(pid)
    stop = await tool.run("stop", {"pid": pid}, ctx(tmp_path))
    assert stop.status is ExecutionStatus.SUCCEEDED, stop.message
    assert stop.evidence["stopped"] is True
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_launched_processes_carry_the_tool_marker(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    probe = "import os, pathlib; pathlib.Path('marker.txt').write_text(os.environ.get('" + (
        TOOL_PROCESS_ENV + "', 'missing'))"
    )
    out = await ProcessTool().run("launch", {"executable": PY, "args": ["-c", probe]}, c)
    assert out.status is ExecutionStatus.SUCCEEDED, out.message
    assert (c.workspace / "marker.txt").read_text() == "1"


async def test_quick_failures_and_missing_programs_are_failed_steps(tmp_path: Path) -> None:
    tool = ProcessTool()
    crash = await tool.run(
        "launch", {"executable": PY, "args": ["-c", "raise SystemExit(3)"]}, ctx(tmp_path)
    )
    assert crash.status is ExecutionStatus.FAILED and crash.exit_code == 3
    missing = await tool.run("launch", {"executable": "no-such-app-x9"}, ctx(tmp_path))
    assert missing.status is ExecutionStatus.FAILED
    assert "not found" in missing.message


@pytest.mark.parametrize(
    "args",
    [
        ["-m", "nomadicos.cli.app", "permissions", "grant-full"],
        ["-m", "nomadicos.api"],
        ["-c", "import nomadicos.authority.store"],
    ],
)
async def test_launch_cannot_reach_owner_surfaces(tmp_path: Path, args: list[str]) -> None:
    with pytest.raises(PathDenied):
        await ProcessTool().run("launch", {"executable": PY, "args": args}, ctx(tmp_path))


async def test_launch_cannot_run_from_protected_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    c = ExecutionContext("task_proc", tmp_path, full_pc=True, protected=(state.resolve(),))
    with pytest.raises(PathDenied):
        await ProcessTool().run(
            "launch", {"executable": PY, "args": ["-c", "pass"], "cwd": str(state)}, c
        )


async def test_stop_refuses_nomadicos_its_host_and_core_processes(tmp_path: Path) -> None:
    tool = ProcessTool()
    with pytest.raises(ProcessDenied):
        await tool.run("stop", {"pid": os.getpid()}, ctx(tmp_path))
    parent = psutil.Process().parent()
    if parent is not None:
        with pytest.raises(ProcessDenied):
            await tool.run("stop", {"pid": parent.pid}, ctx(tmp_path))
    core = next(
        (
            p.pid
            for p in psutil.process_iter(["name"])
            if (p.info.get("name") or "").lower() in {"explorer.exe", "csrss.exe", "svchost.exe"}
        ),
        None,
    )
    if core is not None:
        with pytest.raises(ProcessDenied):
            await tool.run("stop", {"pid": core}, ctx(tmp_path))


async def test_stop_of_a_missing_pid_is_a_failed_step(tmp_path: Path) -> None:
    gone = max(p.pid for p in psutil.process_iter()) + 4_000_000
    out = await ProcessTool().run("stop", {"pid": gone}, ctx(tmp_path))
    assert out.status is ExecutionStatus.FAILED


def test_owner_rules_see_launch_and_window_targets() -> None:
    assert _resource_hint({"executable": "C:/ProjectB/app.exe"}) == "C:/ProjectB/app.exe"
    assert _resource_hint({"window": "Project B - Editor", "name": "Save"}) == (
        "Project B - Editor"
    )
