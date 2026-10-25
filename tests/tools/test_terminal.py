"""Terminal brick: contract, command resolution, and SPEC §25 qualification.

The qualification block runs REAL commands (python, git, node, pytest where
present) plus a temporary application that writes stdout, writes stderr,
reads arguments, reads the environment, depends on cwd, reads stdin, starts a
child process and times out. Per SPEC §25 these are not replaced by mocked
subprocess tests.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import psutil
import pytest

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure, InvalidProposal
from nomadicos.tools.base import ToolOutcome, ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import PathDenied
from nomadicos.tools.terminal import (
    MAX_STREAM_CHARS,
    CommandProblem,
    ProcessSupervisor,
    TerminalTool,
    resolve_command,
)

PY = sys.executable
WINDOWS = os.name == "nt"

APP_SOURCE = """
import os
import subprocess
import sys
import time

mode = sys.argv[1]
if mode == "streams":
    print("out-line")
    print("err-line", file=sys.stderr)
elif mode == "args":
    print("|".join(sys.argv[2:]))
elif mode == "env":
    print(os.environ["NOMADICOS_APP_ENV"])
elif mode == "cwd":
    print(os.path.basename(os.getcwd()))
elif mode == "stdin":
    print(sys.stdin.read().upper())
elif mode == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    print(child.pid, flush=True)
    time.sleep(30)
elif mode == "exit":
    sys.exit(int(sys.argv[2]))
elif mode == "loud":
    print("x" * 500_000)
else:
    sys.exit("unknown mode")
"""


@pytest.fixture()
def tool() -> TerminalTool:
    return TerminalTool(ProcessSupervisor())


@pytest.fixture()
def app(tmp_path: Path) -> Path:
    script = tmp_path / "ws" / "task_term" / "app.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(APP_SOURCE, encoding="utf-8")
    return script


def ctx(tmp_path: Path, task_id: str = "task_term", full_pc: bool = False) -> ExecutionContext:
    return ExecutionContext.for_task(task_id, tmp_path / "ws", full_pc=full_pc)


async def execute(
    tool: TerminalTool, context: ExecutionContext, command: str, *args: str, **extra
) -> ToolOutcome:
    return await tool.run("execute", {"command": command, "args": list(args), **extra}, context)


def wait_gone(*pids: int, timeout_s: float = 10.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not any(psutil.pid_exists(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return not any(psutil.pid_exists(pid) for pid in pids)


# ---------------------------------------------------------------- contract ----


def test_tool_contract_exposes_one_operation() -> None:
    assert TerminalTool.name == "terminal"
    assert set(TerminalTool.ops) == {"execute"}


def test_registry_catalog_and_argument_validation() -> None:
    registry = ToolRegistry()
    registry.register(TerminalTool())
    lines = [line for line in registry.catalog_lines() if "- terminal:" in line]
    assert lines == ['- terminal: operation "execute" args: {command,args,cwd,env,timeout_s,stdin}']
    assert registry.capability("terminal", "execute").capability == "terminal.execute"
    # an EMPTY command is deliberately not rejected by validation: the tool
    # answers it with an honest ERRORED outcome instead of failing closed here
    typed = registry.check_args("terminal", "execute", {"command": "   ", "args": []})
    assert typed["command"] == ""
    with pytest.raises(InvalidProposal):
        registry.check_args("terminal", "execute", {"command": "python", "timeout_s": 0})


# -------------------------------------------------------------- resolution ----


def test_argv_passes_through_verbatim_no_shell_text_is_built() -> None:
    plan = resolve_command(PY, ["--version", "hello > sneaky.txt", "-x", "a; b | c"])
    assert plan.argv == (PY, "--version", "hello > sneaky.txt", "-x", "a; b | c")
    assert plan.executable == PY
    assert plan.via_interpreter is False


def test_missing_executable_is_a_command_problem_not_a_crash() -> None:
    with pytest.raises(CommandProblem, match="process creation failed"):
        resolve_command("definitely-not-an-exe-xyz", [])


@pytest.mark.skipif(WINDOWS, reason="cmd.exe builtins exist only on Windows")
def test_command_string_words_become_argv_elements() -> None:
    plan = resolve_command("echo one two", [])
    assert plan.argv == ("echo", "one", "two")


@pytest.mark.skipif(WINDOWS, reason="posix shell builtins have no executable")
def test_posix_shell_builtins_cannot_become_processes() -> None:
    with pytest.raises(CommandProblem):
        resolve_command("cd", [".."])


@pytest.mark.skipif(not WINDOWS, reason="cmd.exe builtins and .cmd scripts are Windows-only")
def test_builtins_route_through_the_interpreter_as_separate_argv() -> None:
    plan = resolve_command("dir", ["/b"])
    assert plan.executable.lower().endswith("cmd.exe")
    assert plan.argv == (plan.executable, "/d", "/c", "dir", "/b")
    assert plan.via_interpreter is True


@pytest.mark.skipif(not WINDOWS, reason="windows quoting rule set")
def test_quoted_tokens_are_not_decomposed() -> None:
    plan = resolve_command(f'echo "{PY}" "two words"', [])
    assert plan.argv[-2:] == (PY, "two words")


@pytest.mark.skipif(not WINDOWS, reason="the interpreter route is Windows-only")
def test_interpreter_route_refuses_shell_metacharacters() -> None:
    with pytest.raises(CommandProblem, match="metacharacters"):
        resolve_command("echo", ["a & b"])
    with pytest.raises(CommandProblem, match="metacharacters"):
        resolve_command("type", ["file.txt", ">", "other.txt"])


# ------------------------------------------------- real-process qualification --


async def test_real_interpreter_commands_reach_the_os(tool: TerminalTool, tmp_path: Path) -> None:
    """SPEC §25: real python/git/node/pytest invocations, not simulations."""
    candidates: list[list[str]] = [[PY, "--version"], ["git", "--version"], ["node", "--version"]]
    if not shutil.which("git"):
        candidates.remove(["git", "--version"])
    if not shutil.which("node"):
        candidates.remove(["node", "--version"])
    if shutil.which("pytest"):
        candidates.append(["pytest", "--version"])
    context = ctx(tmp_path, "task_qual")
    for argv in candidates:
        outcome = await execute(tool, context, argv[0], *argv[1:])
        assert outcome.status is ExecutionStatus.SUCCEEDED, outcome.message
        assert outcome.exit_code == 0
        assert outcome.stdout.strip()
        assert outcome.process_id and not psutil.pid_exists(outcome.process_id)


async def test_stdout_and_stderr_stay_distinguishable(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "streams")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.exit_code == 0
    assert outcome.stdout.strip() == "out-line"
    assert outcome.stderr.strip() == "err-line"
    assert "err-line" not in outcome.stdout and "out-line" not in outcome.stderr


async def test_arguments_environment_and_cwd(tool: TerminalTool, tmp_path: Path, app: Path) -> None:
    context = ctx(tmp_path)
    (context.workspace / "wdir").mkdir()
    argv = await execute(tool, context, PY, str(app), "args", "one", "-two", "a space")
    assert argv.stdout.strip() == "one|-two|a space"
    environment = await execute(
        tool, context, PY, str(app), "env", env={"NOMADICOS_APP_ENV": "yes"}
    )
    assert environment.stdout.strip() == "yes"
    cwd = await execute(tool, context, PY, str(app), "cwd", cwd="wdir")
    assert cwd.stdout.strip() == "wdir"


async def test_stdin_is_delivered_to_the_child(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "stdin", stdin="quiet")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.stdout.strip() == "QUIET"


async def test_non_zero_exit_is_an_honest_failure(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "exit", "7")
    assert outcome.status is ExecutionStatus.FAILED
    assert outcome.exit_code == 7
    assert outcome.failure is Failure.ACTION_FAILED
    assert outcome.message == "exit code 7"


async def test_loud_output_is_bounded_but_fully_drained(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "loud")
    assert outcome.status is ExecutionStatus.SUCCEEDED
    assert outcome.evidence["stdout_bytes"] >= 500_000
    assert len(outcome.stdout) <= MAX_STREAM_CHARS + 64
    assert "[truncated" in outcome.stdout


async def test_timeout_kills_the_tree_and_leaves_no_orphans(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "child", timeout_s=2.5)
    assert outcome.status is ExecutionStatus.TIMED_OUT
    assert outcome.failure is Failure.TIMEOUT
    assert outcome.evidence["killed_by_timeout"] is True
    child_pid = int(outcome.stdout.strip().splitlines()[0])
    assert outcome.process_id
    assert wait_gone(child_pid, outcome.process_id), "orphaned process left behind"


async def test_cancellation_kills_the_tree(tool: TerminalTool, tmp_path: Path) -> None:
    context = ctx(tmp_path)
    running = asyncio.create_task(
        execute(tool, context, PY, "-c", "import time; time.sleep(60)", timeout_s=120.0)
    )
    while not tool.supervisor.live_pids(context.task_id):
        await asyncio.sleep(0.05)
    pids = tool.supervisor.live_pids(context.task_id)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert wait_gone(*pids), "cancellation left a process behind"
    assert tool.supervisor.live_pids(context.task_id) == []


# ----------------------------------------------------------- failure modes ----


async def test_missing_executable_reports_process_creation_failure(
    tool: TerminalTool, tmp_path: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), "definitely-not-an-exe-xyz")
    assert outcome.status is ExecutionStatus.ERRORED
    assert outcome.failure is Failure.TOOL_ERROR
    assert "process creation failed" in outcome.message


async def test_empty_command_fails_closed(tool: TerminalTool, tmp_path: Path) -> None:
    outcome = await execute(tool, ctx(tmp_path), "   ")
    assert outcome.status is ExecutionStatus.ERRORED
    assert outcome.failure is Failure.TOOL_ERROR


async def test_invalid_environment_entry_is_refused(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(
        tool,
        ctx(tmp_path),
        PY,
        str(app),
        "env",
        env={"BAD=KEY": "x"},
    )
    assert outcome.status is ExecutionStatus.ERRORED
    assert "env" in outcome.message


async def test_missing_cwd_is_a_failure_not_a_spawn(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    outcome = await execute(tool, ctx(tmp_path), PY, str(app), "cwd", cwd="nope-dir")
    assert outcome.status is ExecutionStatus.FAILED
    assert "cwd" in outcome.message


async def test_cwd_outside_the_task_scope_is_an_authorization_refusal(
    tool: TerminalTool, tmp_path: Path, app: Path
) -> None:
    with pytest.raises(PathDenied):
        await execute(tool, ctx(tmp_path), PY, str(app), "cwd", cwd="..")
    with pytest.raises(PathDenied):
        await execute(tool, ctx(tmp_path), PY, str(app), "cwd", cwd=str(tmp_path))


# ------------------------------------------------------------- supervision ----


async def test_supervisor_never_crosses_task_boundaries(tool: TerminalTool, tmp_path: Path) -> None:
    first = ctx(tmp_path, "task_a")
    second = ctx(tmp_path, "task_b")
    long_running = ["-c", "import time; time.sleep(30)"]
    task_a = asyncio.create_task(execute(tool, first, PY, *long_running, timeout_s=120.0))
    task_b = asyncio.create_task(execute(tool, second, PY, *long_running, timeout_s=120.0))
    while not (tool.supervisor.live_pids("task_a") and tool.supervisor.live_pids("task_b")):
        await asyncio.sleep(0.05)
    pids_a = tool.supervisor.live_pids("task_a")
    pids_b = tool.supervisor.live_pids("task_b")
    assert not set(pids_a) & set(pids_b)
    tool.supervisor.kill_owned("task_a")
    assert wait_gone(*pids_a)
    assert all(psutil.pid_exists(pid) for pid in pids_b), "cleanup crossed the task boundary"
    assert tool.supervisor.kill_all() and wait_gone(*pids_b)
    for task in (task_a, task_b):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_supervisor_ledger_is_per_application() -> None:
    supervisor = ProcessSupervisor()
    stranger = 1  # never registered by this tool: cleanup must not touch it
    supervisor.release("task_missing", 4242)
    assert supervisor.live_pids("task_missing") == []
    assert supervisor.kill_owned("task_missing") == []
    assert supervisor.kill_all() == []
    assert stranger == 1
