"""Phase 13D durable execution ledger: crash/restart replay safety.

The ledger only answers "has this action identity already been recorded?".
Every case here proves the same logical authorized action CANNOT execute
twice across a simulated restart, and that the ledger stays DATA."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomadicos.action_ir import ProposalValidator, ValidationContext, parse_model_output
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.policy import CapabilityPolicy
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.executor.dispatch import Executor
from nomadicos.kernel.errors import RevokedAuthority
from nomadicos.kernel.events import EventLogger
from nomadicos.persistence import PersistenceCorrupt, PersistenceError
from nomadicos.persistence.errors import PersistenceUnavailable
from nomadicos.persistence.ledger import DurableFileLedger, make_execution_ledger
from nomadicos.tools import (
    DesktopTool,
    ExecutionContext,
    FilesystemTool,
    ToolRegistry,
)

PATTERNS = ["filesystem.*", "terminal.*", "desktop.*", "git.*", "process.*"]


class _Persistence:
    """Minimal PersistenceConfig stand-in for the factory (dsn + state_dir)."""

    def __init__(self, state_dir: Path, dsn: str | None = None) -> None:
        self.dsn = dsn
        self.state_dir = str(state_dir)


class Chain:
    """Production pipeline with the DURABLE ledger wired (Phase 13D)."""

    def __init__(self, state_dir: Path, backend: FakeDesktopBackend | None = None) -> None:
        self.log = EventLogger()
        self.store = AuthorityStore(state_dir / "authority.json")
        self.backend = backend or FakeDesktopBackend(width=320, height=200)
        reg = ToolRegistry()
        reg.register(FilesystemTool())
        reg.register(DesktopTool(self.backend))
        self.tools = reg
        self.validator = ProposalValidator(reg, self.log)
        policy = CapabilityPolicy(self.store, granted_patterns=list(PATTERNS))
        self.authz = AuthorizationService(self.store, policy, self.log)
        self.executor = Executor(
            reg, self.store, self.log, ledger=make_execution_ledger(_Persistence(state_dir))
        )

    def ctx(self, root: Path, task_id: str = "task_alpha") -> ExecutionContext:
        return ExecutionContext.for_task(task_id, root / task_id)

    def parse(self, raw: str, ctx: ExecutionContext):
        return parse_model_output(
            raw, task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
        )

    def validate(self, parsed, ctx: ExecutionContext):
        return self.validator.validate(
            parsed,
            ValidationContext(
                task_id=ctx.task_id, step_id="step_1", model_id="test-model", attempt=1
            ),
        )

    async def drive(self, raw: str, ctx: ExecutionContext):
        parsed = self.parse(raw, ctx)
        if parsed.is_claim:
            return parsed.value
        authorized = self.authz.authorize(parsed.value, self.validate(parsed.value, ctx))
        return await self.executor.execute(authorized, ctx)


def desktop_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "desktop", "operation": operation, "args": args})


def fs_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "filesystem", "operation": operation, "args": args})


def granted(state_dir: Path, backend: FakeDesktopBackend | None = None) -> Chain:
    chain = Chain(state_dir, backend)
    chain.store.grant_full_pc_autonomy()
    return chain


# ------------------------------------------------- CASE 1: restart + replay -


async def test_case1_restart_duplicate_blocked_no_second_side_effect(tmp_path: Path) -> None:
    backend_a = FakeDesktopBackend(width=320, height=200)
    chain_a = granted(tmp_path, backend_a)
    ctx_a = chain_a.ctx(tmp_path, "task_dupe")
    first = await chain_a.drive(desktop_json("mouse_click", {"x": 5, "y": 6}), ctx_a)
    assert first.status is ExecutionStatus.SUCCEEDED
    assert backend_a.clicks == [(5, 6, "left", 1)]

    # "process restart": fresh backend, fresh executor, SAME durable ledger
    backend_b = FakeDesktopBackend(width=320, height=200)
    chain_b = granted(tmp_path, backend_b)
    ctx_b = chain_b.ctx(tmp_path, "task_dupe")
    replay = await chain_b.drive(desktop_json("mouse_click", {"x": 5, "y": 6}), ctx_b)
    assert replay.evidence["duplicate_blocked"] is True
    assert backend_b.clicks == [], "no second side effect after restart"


async def test_case2_fresh_action_executes_after_restart(tmp_path: Path) -> None:
    chain_a = granted(tmp_path)
    await chain_a.drive(fs_json("write", {"path": "a.txt", "content": "1"}), chain_a.ctx(tmp_path))

    chain_b = granted(tmp_path)
    ctx_b = chain_b.ctx(tmp_path, "task_fresh")
    result = await chain_b.drive(fs_json("write", {"path": "b.txt", "content": "2"}), ctx_b)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert (ctx_b.workspace / "b.txt").read_text(encoding="utf-8") == "2"


# ------------------------------------------- CASE 3: stale epoch + restart --


async def test_case3_stale_epoch_rejected_after_restart(tmp_path: Path) -> None:
    chain_a = granted(tmp_path)
    ctx_a = chain_a.ctx(tmp_path, "task_stale")
    raw = fs_json("write", {"path": "s.txt", "content": "x"})
    parsed = chain_a.parse(raw, ctx_a)
    authorized = chain_a.authz.authorize(parsed.value, chain_a.validate(parsed.value, ctx_a))
    await chain_a.executor.execute(authorized, ctx_a)

    chain_b = granted(tmp_path)
    chain_b.store.revoke_all()  # epoch changed while stopped
    ctx_b = chain_b.ctx(tmp_path, "task_stale")
    with pytest.raises(RevokedAuthority):
        await chain_b.executor.execute(authorized, ctx_b)
    # the stale grant never executed in the restarted process
    assert not [
        e
        for e in chain_b.log.events("task_stale")
        if e.type.value == "TOOL_EXECUTED" and e.result == "SUCCEEDED"
    ]


# ------------------------------------------------ CASE 4: task isolation ----


async def test_case4_identical_ops_in_different_tasks_both_execute(tmp_path: Path) -> None:
    chain = granted(tmp_path)
    r1 = await chain.drive(
        fs_json("write", {"path": "same.txt", "content": "x"}),
        chain.ctx(tmp_path, "task_one"),
    )
    r2 = await chain.drive(
        fs_json("write", {"path": "same.txt", "content": "x"}),
        chain.ctx(tmp_path, "task_two"),
    )
    assert r1.status is ExecutionStatus.SUCCEEDED
    assert r2.status is ExecutionStatus.SUCCEEDED
    assert r2.evidence.get("duplicate_blocked") is None


# ------------------------------------------------ CASE 5: corrupt ledger ----


async def test_case5_corrupt_ledger_fails_closed_never_replays(tmp_path: Path) -> None:
    chain = granted(tmp_path)
    ctx = chain.ctx(tmp_path, "task_corrupt")
    await chain.drive(fs_json("write", {"path": "c.txt", "content": "x"}), ctx)
    ledger_file = tmp_path / "persistence" / "execution-ledger.json"
    raw = json.loads(ledger_file.read_text(encoding="utf-8"))
    raw["entries"]["0" * 32] = {"broken": "not a result"}
    ledger_file.write_text(json.dumps(raw), encoding="utf-8")
    # a corrupt entry must fail closed - never be treated as "not executed"
    with pytest.raises(PersistenceCorrupt):
        DurableFileLedger(tmp_path).get("0" * 32)


# -------------------------------------------- CASE 6: unavailable ledger ----


async def test_case6_unavailable_persistence_is_structured(tmp_path: Path) -> None:
    chain = granted(tmp_path)
    ctx = chain.ctx(tmp_path, "task_unavail")

    class DownLedger:
        def get(self, key):
            raise PersistenceUnavailable("durable ledger down")

        def put(self, key, result):
            raise PersistenceUnavailable("durable ledger down")

    executor = Executor(chain.tools, chain.store, chain.log, ledger=DownLedger())
    parsed = chain.parse(fs_json("write", {"path": "u.txt", "content": "x"}), ctx)
    authorized = chain.authz.authorize(parsed.value, chain.validate(parsed.value, ctx))
    # a fail-closed ledger read surfaces as a structured persistence error
    with pytest.raises(PersistenceError):
        await executor.execute(authorized, ctx)
    assert not (ctx.workspace / "u.txt").exists()


# --------------------------------------------------- crash window: honest ---


async def test_crash_window_side_effect_without_durable_record(tmp_path: Path) -> None:
    """CRITICAL: the side effect happens but the durable ledger cannot
    record it. The result is returned honestly flagged: replay protection
    is NOT guaranteed, nothing retries, nothing claims durable recording."""
    chain = granted(tmp_path)
    ctx = chain.ctx(tmp_path, "task_window")

    class BrokenPutLedger:
        def __init__(self) -> None:
            self._inner = DurableFileLedger(tmp_path)

        def get(self, key):
            return self._inner.get(key)

        def put(self, key, result):
            raise PersistenceUnavailable("disk full: ledger write failed")

    executor = Executor(chain.tools, chain.store, chain.log, ledger=BrokenPutLedger())
    parsed = chain.parse(fs_json("write", {"path": "w.txt", "content": "x"}), ctx)
    authorized = chain.authz.authorize(parsed.value, chain.validate(parsed.value, ctx))
    result = await executor.execute(authorized, ctx)
    # the side effect DID happen (file written)...
    assert (ctx.workspace / "w.txt").read_text(encoding="utf-8") == "x"
    # ...and the result does NOT claim durable protection
    assert result.evidence["durable_ledger"] == "unrecorded"
    assert "disk full" in result.evidence["ledger_error"]
    # nothing retried automatically; the ledger holds no record
    assert DurableFileLedger(tmp_path).get(
        __import__("hashlib").sha256(parsed.value.fingerprint().encode()).hexdigest()[:32]
    ) is None


# ------------------------------------- side-effect duplicate protection -----


async def test_desktop_and_filesystem_duplicate_protection(tmp_path: Path) -> None:
    chain = granted(tmp_path)
    ctx = chain.ctx(tmp_path, "task_dupe2")
    r1 = await chain.drive(desktop_json("press_key", {"key": "ctrl+s"}), ctx)
    r2 = await chain.drive(desktop_json("press_key", {"key": "ctrl+s"}), ctx)
    assert r1.status is ExecutionStatus.SUCCEEDED and r2.evidence["duplicate_blocked"] is True
    assert chain.backend.keys == ["ctrl+s"]  # typed exactly once
    f1 = await chain.drive(fs_json("write", {"path": "d.txt", "content": "x"}), ctx)
    f2 = await chain.drive(fs_json("write", {"path": "d.txt", "content": "x"}), ctx)
    assert f1.status is ExecutionStatus.SUCCEEDED and f2.evidence["duplicate_blocked"] is True


async def test_terminal_execution_identity_is_respected(tmp_path: Path) -> None:
    import sys

    from nomadicos.tools import ProcessSupervisor, TerminalTool

    chain = granted(tmp_path)
    reg = chain.tools
    reg.register(TerminalTool(ProcessSupervisor()))
    ctx = chain.ctx(tmp_path, "task_term")
    raw = json.dumps(
        {
            "tool": "terminal",
            "operation": "execute",
            "args": {"command": sys.executable, "args": ["--version"]},
        }
    )
    parsed = chain.parse(raw, ctx)
    authorized = chain.authz.authorize(parsed.value, chain.validate(parsed.value, ctx))
    first = await chain.executor.execute(authorized, ctx)
    assert first.status is ExecutionStatus.SUCCEEDED
    # same authorized action re-submitted: durably blocked, not re-executed
    second = await chain.executor.execute(authorized, ctx)
    assert second.evidence["duplicate_blocked"] is True


# --------------------------------------------------- bounded + guarded ------


def test_ledger_capacity_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    from nomadicos.contracts.execution import ExecutionResult
    from nomadicos.persistence import ledger as ledger_module

    monkeypatch.setattr(ledger_module, "MAX_LEDGER_ENTRIES", 3)
    ledger = DurableFileLedger(tmp_path)

    def result_for(i: int) -> ExecutionResult:
        return ExecutionResult(
            task_id=f"task_{i:020d}",
            step_id="s",
            action_id="a",
            action_fingerprint="f",
            model_id="m",
            tool="filesystem",
            operation="read",
            status="SUCCEEDED",
        )

    for i in range(3):
        ledger.put(hashlib.sha256(f"key-{i}".encode()).hexdigest()[:32], result_for(i))
    with pytest.raises(PersistenceError, match="capacity"):
        ledger.put(hashlib.sha256(b"key-new").hexdigest()[:32], result_for(99))


def test_ledger_is_data_only_source_guard() -> None:
    import inspect

    from nomadicos.persistence import ledger as module

    source = inspect.getsource(module)
    for token in (
        "AuthorizedAction(",
        "TaskStatus.SUCCESS",
        "grant_full",
        "revoke_all",
        "answer_conflict",
        "PolicyEngine",
        "subprocess",
        "Popen",
        "os.system",
        "urllib",
        "requests",
        "run(",
    ):
        assert token not in source, token


async def test_duplicate_block_writes_no_success(tmp_path: Path) -> None:
    backend = FakeDesktopBackend(width=320, height=200)
    chain = granted(tmp_path, backend)
    ctx = chain.ctx(tmp_path, "task_nosuccess")
    await chain.drive(desktop_json("mouse_move", {"x": 1, "y": 2}), ctx)
    replay = await chain.drive(desktop_json("mouse_move", {"x": 1, "y": 2}), ctx)
    assert replay.evidence["duplicate_blocked"] is True
    results = [e.result for e in chain.log.events("task_nosuccess")]
    assert "SUCCESS" not in results
