"""Phase 6: production path — IR -> validation -> authority -> executor.

Tests here use REAL OS processes and the REAL filesystem inside temporary
workspaces (SPEC §25, §39, §48: mocks do not replace production-path proof).
Proposals entering the pipeline are raw model-shaped STRINGS (manually
authored unless a test is marked hardware/model-authored).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from nomadicos.action_ir import ProposalValidator, ValidationContext, parse_model_output
from nomadicos.authority import AuthorityStore, AuthorizationService, CapabilityPolicy
from nomadicos.contracts.action import ActionProposal, AuthorizedAction
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.executor import Executor
from nomadicos.kernel.errors import (
    AuthorizationDenied,
    Failure,
    InvalidProposal,
    RevokedAuthority,
)
from nomadicos.kernel.events import EventLogger, EventType
from nomadicos.tools import (
    ExecutionContext,
    FilesystemTool,
    ProcessSupervisor,
    TerminalTool,
    ToolRegistry,
)

PATTERNS = ["filesystem.*", "terminal.*", "git.*", "process.*", "desktop.*"]


class Chain:
    """The production pipeline wired exactly once, as any caller would."""

    def __init__(self, tmp_path: Path, *, denied: list[str] | None = None) -> None:
        self.log = EventLogger()
        self.store = AuthorityStore(tmp_path / "state" / "authority.json")
        tool_reg = ToolRegistry()
        self.terminal = TerminalTool(ProcessSupervisor())
        tool_reg.register(FilesystemTool())
        tool_reg.register(self.terminal)
        self.tools = tool_reg
        self.validator = ProposalValidator(tool_reg, self.log)
        policy = CapabilityPolicy(
            self.store, granted_patterns=PATTERNS, hard_denied_resources=denied or []
        )
        self.authz = AuthorizationService(self.store, policy, self.log)
        self.executor = Executor(tool_reg, self.store, self.log)

    def ctx(
        self, tmp_path: Path, task_id: str = "task_alpha", full_pc: bool = False
    ) -> ExecutionContext:
        return ExecutionContext.for_task(task_id, tmp_path / "ws", full_pc=full_pc)

    async def authorize(self, raw: str, ctx: ExecutionContext):
        parsed = parse_model_output(
            raw, task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
        )
        if parsed.is_claim:
            return parsed.value
        cap = self.validator.validate(
            parsed.value,
            ValidationContext(
                task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
            ),
        )
        return self.authz.authorize(parsed.value, cap)

    async def drive(self, raw: str, ctx: ExecutionContext):
        authorized = await self.authorize(raw, ctx)
        if isinstance(authorized, AuthorizedAction):
            return await self.executor.execute(authorized, ctx)
        return authorized


def fs_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "filesystem", "operation": operation, "args": args})


def term_exec(command: str, *args: str, timeout: float | None = None, **extra) -> str:
    payload: dict = {"command": command, "args": list(args), **extra}
    if timeout is not None:
        payload["timeout_s"] = timeout
    return json.dumps({"tool": "terminal", "operation": "execute", "args": payload})


@pytest.fixture()
def chain(tmp_path: Path) -> Chain:
    return Chain(tmp_path)


@pytest.fixture()
def granted(chain: Chain) -> Chain:
    chain.store.grant_full_pc_autonomy()
    return chain


# ------------------------------------------------- real filesystem path ---


async def test_e2e_model_shaped_string_writes_and_reads_real_file(
    granted: Chain, tmp_path: Path
) -> None:
    ctx = granted.ctx(tmp_path)
    fenced = (
        "Sure, creating it now:\n```json\n"
        + fs_json("write", {"path": "notes.txt", "content": "hello real disk"})
        + "\n```"
    )
    result = await granted.drive(fenced, ctx)
    assert result.status is ExecutionStatus.SUCCEEDED
    target = ctx.workspace / "notes.txt"
    assert target.read_text(encoding="utf-8") == "hello real disk"
    assert result.evidence["sha256"]
    assert result.model_id == "test-model" and result.task_id == ctx.task_id
    read = await granted.drive(fs_json("read", {"path": "notes.txt"}), ctx)
    assert read.status is ExecutionStatus.SUCCEEDED
    assert read.evidence["content"] == "hello real disk"


async def test_e2e_list_move_delete(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    await granted.drive(fs_json("write", {"path": "a.txt", "content": "1"}), ctx)
    r = await granted.drive(fs_json("list", {"directory": "."}), ctx)
    assert [e["name"] for e in r.evidence["entries"]] == ["a.txt"]
    r = await granted.drive(fs_json("move", {"source": "a.txt", "target": "b.txt"}), ctx)
    assert r.status is ExecutionStatus.SUCCEEDED and not (ctx.workspace / "a.txt").exists()
    r = await granted.drive(fs_json("delete", {"path": "b.txt"}), ctx)
    assert r.status is ExecutionStatus.SUCCEEDED and r.evidence["exists_after"] is False
    again = await granted.drive(fs_json("delete", {"path": "b.txt"}), ctx)
    # Phase 13D: the identical proposal is the SAME action identity - the
    # durable ledger blocks re-execution instead of repeating the side effect.
    assert again.evidence["duplicate_blocked"] is True
    # a genuinely fresh action (different task identity) still executes and
    # honestly fails on the missing file
    fresh = await granted.drive(
        fs_json("delete", {"path": "b.txt"}), granted.ctx(tmp_path, "task_alpha2")
    )
    assert fresh.status is ExecutionStatus.FAILED
    assert fresh.failure is Failure.ACTION_FAILED


# ------------------------------------------------------ real terminal -----


async def test_terminal_real_processes_stdout_stderr_exit(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    version = await granted.drive(term_exec(sys.executable, "--version"), ctx)
    assert version.status is ExecutionStatus.SUCCEEDED
    assert version.exit_code == 0 and "Python 3" in version.stdout
    for _ in range(20):
        if not psutil.pid_exists(version.process_id):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(version.process_id)  # cleaned up
    mixed = await granted.drive(
        term_exec(
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ),
        ctx,
    )
    assert mixed.status is ExecutionStatus.FAILED and mixed.exit_code == 3
    assert mixed.stdout.strip() == "out" and mixed.stderr.strip() == "err"
    assert mixed.failure is Failure.ACTION_FAILED


async def test_terminal_args_env_cwd_stdin(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    (ctx.workspace / "wdir").mkdir()
    argv = await granted.drive(
        term_exec(
            sys.executable,
            "-c",
            "import sys; print('|'.join(sys.argv[1:]))",
            "one",
            "-two",
        ),
        ctx,
    )
    assert argv.stdout.strip() == "one|-two"
    env = await granted.drive(
        term_exec(
            sys.executable,
            "-c",
            "import os; print(os.environ['NOMADICOS_TEST'])",
            env={"NOMADICOS_TEST": "yes"},
        ),
        ctx,
    )
    assert env.stdout.strip() == "yes"
    cwd = await granted.drive(
        term_exec(sys.executable, "-c", "import os; print(os.getcwd())", cwd="wdir"), ctx
    )
    assert cwd.stdout.strip().lower().replace("\\", "/").endswith("task_alpha/wdir")
    stdin = await granted.drive(
        term_exec(
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read().upper())",
            stdin="quiet",
        ),
        ctx,
    )
    assert stdin.stdout.strip() == "QUIET"


async def test_terminal_metacharacters_are_inert_argv_data(granted: Chain, tmp_path: Path) -> None:
    """No shell: ' > file' cannot smuggle redirection (§6.5)."""
    ctx = granted.ctx(tmp_path)
    r = await granted.drive(
        term_exec(sys.executable, "-c", "import sys; print(sys.argv[1])", "hello > sneaky.txt"),
        ctx,
    )
    assert r.status is ExecutionStatus.SUCCEEDED
    assert r.stdout.strip() == "hello > sneaky.txt"
    assert not list(ctx.workspace.rglob("sneaky.txt"))


async def test_terminal_timeout_kills_process_tree_no_orphans(
    granted: Chain, tmp_path: Path
) -> None:
    ctx = granted.ctx(tmp_path)
    child_holding = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        "print(p.pid, flush=True); time.sleep(120)"
    )
    result = await granted.drive(term_exec(sys.executable, "-c", child_holding, timeout=3.0), ctx)
    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.failure is Failure.TIMEOUT
    child_pid = int(result.stdout.strip().splitlines()[0])
    assert result.process_id
    deadline = time.time() + 5
    while time.time() < deadline and (
        psutil.pid_exists(child_pid) or psutil.pid_exists(result.process_id)
    ):
        time.sleep(0.2)
    assert not psutil.pid_exists(child_pid), "orphaned child left behind"
    assert not psutil.pid_exists(result.process_id)


# ------------------------------------------------------- executor gates ---


async def test_duplication_single_use_blocked(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    raw = fs_json("create", {"path": "once.txt", "content": "first"})
    aa = await granted.authorize(raw, ctx)
    first = await granted.executor.execute(aa, ctx)
    assert first.status is ExecutionStatus.SUCCEEDED
    second = await granted.executor.execute(aa, ctx)
    assert second.evidence["duplicate_blocked"] is True
    # re-execution was refused outright (create would have hit FILE_EXISTS)
    assert (ctx.workspace / "once.txt").read_text(encoding="utf-8") == "first"
    assert any(
        e.type is EventType.TOOL_EXECUTED and e.result == "DUPLICATE_BLOCKED"
        for e in granted.log.events(ctx.task_id)
    )


async def test_stale_grant_after_revoke_refused_without_side_effect(
    granted: Chain, tmp_path: Path
) -> None:
    ctx = granted.ctx(tmp_path)
    aa = await granted.authorize(fs_json("write", {"path": "never.txt", "content": "x"}), ctx)
    granted.store.revoke_all()
    with pytest.raises(RevokedAuthority):
        await granted.executor.execute(aa, ctx)
    assert not (ctx.workspace / "never.txt").exists()


async def test_tampered_artifact_refused(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    aa = await granted.authorize(fs_json("write", {"path": "ok.txt", "content": "x"}), ctx)
    aa.proposal.operation = "delete"  # mutate AFTER authorization
    with pytest.raises(Exception, match="integrity"):
        await granted.executor.execute(aa, ctx)
    assert not (ctx.workspace / "ok.txt").exists()


async def test_unknown_capability_denied_before_minting(granted: Chain, tmp_path: Path) -> None:
    from nomadicos.contracts.action import CapabilityRef

    ctx = granted.ctx(tmp_path)
    parsed = parse_model_output(
        json.dumps({"tool": "rmboss", "operation": "launch", "args": {}}),
        task_id=ctx.task_id,
        step_id="step_1",
        model_id="test-model",
    )
    assert isinstance(parsed.value, ActionProposal)
    with pytest.raises(AuthorizationDenied, match="not inside active profile"):
        await granted.authz.authorize(parsed.value, CapabilityRef(capability="rmboss.launch"))


async def test_path_escape_denied_in_task_scope_and_sibling_prefix_fails(
    granted: Chain, tmp_path: Path
) -> None:
    ctx = granted.ctx(tmp_path)
    with pytest.raises(AuthorizationDenied):
        await granted.drive(fs_json("write", {"path": "../outside.txt", "content": "x"}), ctx)
    assert not (tmp_path / "outside.txt").exists()
    from nomadicos.tools.paths import resolve_scoped

    with pytest.raises(AuthorizationDenied):
        resolve_scoped(str(tmp_path / "ws" / "task_alpha-secret.txt"), ctx)
    outside = tmp_path / "elsewhere.txt"
    with pytest.raises(AuthorizationDenied):
        await granted.drive(fs_json("write", {"path": str(outside), "content": "x"}), ctx)
    full_ctx = granted.ctx(tmp_path, task_id="task_full", full_pc=True)
    r = await granted.drive(
        fs_json("write", {"path": str(outside), "content": "broad but attributed"}), full_ctx
    )
    assert r.status is ExecutionStatus.SUCCEEDED
    assert outside.read_text(encoding="utf-8") == "broad but attributed"


@pytest.mark.skipif(os.name != "nt" and not hasattr(os, "symlink"), reason="no symlink API")
async def test_symlink_cannot_launder_escape(granted: Chain, tmp_path: Path) -> None:
    """Junctions/symlinks resolve BEFORE confinement checks (SPEC §6.4)."""
    ctx = granted.ctx(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-read", encoding="utf-8")
    link = ctx.workspace / "innocent.txt"
    try:
        os.symlink(secret, link)
    except OSError as exc:  # symlink needs privilege on Windows: honest skip
        pytest.skip(f"symlink privilege unavailable: {exc}")
    with pytest.raises(AuthorizationDenied):
        await granted.drive(fs_json("read", {"path": "innocent.txt"}), ctx)


# ------------------------------------------------ failure structured ------


async def test_terminal_real_failures_honest_results(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    missing = await granted.drive(term_exec("definitely-not-an-exe-xyz"), ctx)
    assert missing.status is ExecutionStatus.ERRORED
    assert missing.failure is Failure.TOOL_ERROR
    assert "process creation failed" in missing.message
    badcwd = await granted.drive(term_exec(sys.executable, "--version", cwd="nope-dir"), ctx)
    assert badcwd.status is ExecutionStatus.FAILED
    assert "cwd" in badcwd.message
    empty = await granted.drive(term_exec("   "), ctx)
    assert empty.status is ExecutionStatus.ERRORED


async def test_malformed_args_and_actions_never_execute(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    with pytest.raises(InvalidProposal):
        await granted.drive(fs_json("write", {"path": 1}), ctx)
    with pytest.raises(InvalidProposal):
        await granted.drive(json.dumps({"tool": "nosuch", "operation": "x"}), ctx)
    assert not list(ctx.workspace.iterdir())


async def test_no_grant_denies_everything(granted: Chain, tmp_path: Path) -> None:
    granted.store.revoke_all()
    ctx = granted.ctx(tmp_path)
    with pytest.raises(AuthorizationDenied, match="no owner grant"):
        await granted.drive(fs_json("write", {"path": "x.txt", "content": "y"}), ctx)


# ------------------------------------------------------- audit events -----


async def test_events_record_full_trail(granted: Chain, tmp_path: Path) -> None:
    ctx = granted.ctx(tmp_path)
    await granted.drive(fs_json("write", {"path": "t.txt", "content": "audit"}), ctx)
    types = [e.type for e in granted.log.events(ctx.task_id)]
    for expected in (
        EventType.ACTION_PROPOSED,
        EventType.AUTHORIZATION_GRANTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_EXECUTED,
    ):
        assert expected in types


# --------------------------------------------- process ownership (§6.7) ---


def test_supervisor_kills_only_its_tasks_processes() -> None:
    sup = ProcessSupervisor()
    a = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    b = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    sup.register("task_a", a.pid)
    sup.register("task_b", b.pid)
    killed = sup.kill_owned("task_a")
    assert killed == [a.pid]
    a.wait(timeout=10)
    assert psutil.pid_exists(b.pid)  # other task untouched
    sup.kill_owned("task_b")
    b.wait(timeout=10)
    assert not psutil.pid_exists(b.pid)


def test_executor_import_is_dispatch_only() -> None:
    """Structural guard: no model/provider access from the executor."""
    import inspect

    from nomadicos.executor import dispatch as dispatch_module

    src = inspect.getsource(dispatch_module)
    for forbidden in ("nomadicos.inference", "nomadicos.router", "generate(", "grant_full"):
        assert forbidden not in src, f"executor reached inference: {forbidden}"


async def test_completion_claim_never_executes(granted: Chain, tmp_path: Path) -> None:
    """finished=true stays a claim (SPEC §28); Phase 6 mints no SUCCESS."""
    ctx = granted.ctx(tmp_path)
    from nomadicos.contracts.action import CompletionClaim

    out = await granted.drive(json.dumps({"finished": True}), ctx)
    assert isinstance(out, CompletionClaim) and out.claim is True
    assert out.task_id == ctx.task_id
    assert not list(ctx.workspace.iterdir())
    results = [e.result for e in granted.log.events(ctx.task_id)]
    assert all(r != "SUCCESS" for r in results if r)
