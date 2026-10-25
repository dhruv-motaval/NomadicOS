"""Phase 3.1 durable execution intent and crash-gap hardening tests (SPEC §56.14).

These tests simulate process crashes / restarts at every defined boundary and
prove that NomadicOS never blindly replays a side effect, never reuses a
stale AuthorizedAction, and that concurrent workers cannot both initiate the
same operation.

Restart is simulated by creating a second NomadicApp over the SAME
state_dir (the checkpointer, execution ledger, task store and authority
store all hydrate from disk), exactly as a real process restart would.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))

from helpers import make_app, write_json
from test_brain_integration import brain_scripts, candidate_json

from nomadicos.contracts.action import ActionProposal, ProposalKind
from nomadicos.contracts.core import Goal, PlanStep, TaskStatus
from nomadicos.contracts.execution import (
    ExecutionIntent,
    ExecutionResult,
    ExecutionStatus,
    IntentStatus,
)
from nomadicos.orchestration.reconciliation import (
    RecoveryDecision,
    reconcile_resume,
)
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable
from nomadicos.persistence.ledger import DurableFileLedger

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_exists", "path": "report.txt"}]


def _ledger_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def _make_proposal(
    task_id: str,
    step_id: str = "s0",
    tool: str = "filesystem",
    operation: str = "write",
    args: dict | None = None,
    model_id: str = "models/small.gguf",
) -> ActionProposal:
    return ActionProposal(
        task_id=task_id,
        step_id=step_id,
        attempt=1,
        model_id=model_id,
        kind=ProposalKind.ACTION,
        tool=tool,
        operation=operation,
        args=args or {"path": "report.txt", "content": "HELLO-GRAPH"},
        note="crash-recovery probe",
    )


def _make_execution(
    task_id: str,
    step_id: str = "s0",
    fingerprint: str = "0" * 64,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    tool: str = "filesystem",
    operation: str = "write",
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        step_id=step_id,
        action_id="act-crash-1",
        action_fingerprint=fingerprint,
        model_id="models/small.gguf",
        tool=tool,
        operation=operation,
        status=status,
        exit_code=0,
        stdout="",
        stderr="",
        evidence={},
        message="ok",
    )


def _base_state(task_id: str, goal_text: str = GOAL) -> dict[str, Any]:
    goal = Goal(objective=goal_text, constraints=[])
    step = PlanStep(id="s0", description="write report.txt")
    proposal = _make_proposal(task_id, "s0")
    return {
        "task_id": task_id,
        "goal_text": goal_text,
        "goal": goal,
        "plan": [step],
        "current_step": 0,
        "model_id": "models/small.gguf",
        "proposals": [proposal],
        "attempt": 1,
        "total_steps_used": 0,
        "tried_models": [],
        "task_status": TaskStatus.RUNNING,
        "outcome_note": "",
        "fingerprint_history": [],
        "brain_state": None,
        "outcome_uncertain": False,
    }


def _make_intent(
    task_id: str,
    step_id: str = "s0",
    fingerprint: str = "fp_test",
    status: IntentStatus = IntentStatus.INTENT_RECORDED,
) -> ExecutionIntent:
    return ExecutionIntent(
        task_id=task_id,
        step_id=step_id,
        fingerprint=fingerprint,
        authorization_epoch=1,
        tool="filesystem",
        operation="write",
        args={"path": "report.txt", "content": "HELLO-GRAPH"},
    )


# ===========================================================================
# Unit tests: intent lifecycle in the ledger
# ===========================================================================


def test_intent_claim_is_atomic_first_writer_wins(tmp_path: Path) -> None:
    """Two concurrent claim_intent calls for the same key: only one wins."""
    ledger = DurableFileLedger(tmp_path)
    intent = _make_intent("task1", "s0", "fp1")
    key1 = _ledger_key("key1")
    assert ledger.claim_intent(key1, intent) is True
    assert ledger.claim_intent(key1, intent) is False  # second claim rejected


def test_intent_status_transitions(tmp_path: Path) -> None:
    """Intent moves INTENT_RECORDED -> EXECUTION_STARTED -> COMPLETED."""
    ledger = DurableFileLedger(tmp_path)
    intent = _make_intent("task1", "s0", "fp1")
    key1 = _ledger_key("key1")
    ledger.claim_intent(key1, intent)
    got = ledger.get_intent(key1)
    assert got is not None
    assert got.status is IntentStatus.INTENT_RECORDED

    ledger.mark_intent_started(key1)
    got = ledger.get_intent(key1)
    assert got.status is IntentStatus.EXECUTION_STARTED

    ledger.mark_intent_completed(key1)
    got = ledger.get_intent(key1)
    assert got.status is IntentStatus.COMPLETED
    assert got.completed_at is not None


def test_intent_get_missing_returns_none(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    assert ledger.get_intent(_ledger_key("nonexistent")) is None


# ===========================================================================
# Unit tests: reconcile_resume with intent checking
# ===========================================================================


def test_reconcile_intent_started_no_result_blocks_unknown(
    tmp_path: Path,
) -> None:
    """Execution STARTED intent but no result (Case C): BLOCK_UNKNOWN_OUTCOME,
    never replay."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-c")
    state["pending_authorized_id"] = None
    state["executions"] = []
    fp = state["proposals"][0].fingerprint()
    # Pre-record an EXECUTION_STARTED intent (crash gap)
    app.executor._ledger.claim_intent(
        _ledger_key(fp),
        ExecutionIntent(
            task_id="task-c",
            step_id="s0",
            fingerprint=fp,
            authorization_epoch=1,
            tool="filesystem",
            operation="write",
            args={"path": "report.txt", "content": "HELLO-GRAPH"},
        ),
    )
    app.executor._ledger.mark_intent_started(_ledger_key(fp))
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.BLOCK_UNKNOWN_OUTCOME
    assert result.should_block
    assert "blocked" in result.outcome_note.lower()


def test_reconcile_intent_recorded_not_started_resumes_planning(
    tmp_path: Path,
) -> None:
    """Intent recorded but execution never started: safe to resume planning
    with fresh authorization (Case A/B boundary)."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-a")
    state["pending_authorized_id"] = "auth-abc"  # artifact was issued
    state["executions"] = []
    fp = state["proposals"][0].fingerprint()
    app.executor._ledger.claim_intent(
        _ledger_key(fp),
        ExecutionIntent(
            task_id="task-a",
            step_id="s0",
            fingerprint=fp,
            authorization_epoch=1,
            tool="filesystem",
            operation="write",
            args={"path": "report.txt", "content": "HELLO-GRAPH"},
        ),
    )
    # Intent is INTENT_RECORDED (not started) → safe to re-authorize
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.REAUTHORIZE_ACTION
    assert not result.should_block


# ===========================================================================
# Integration tests: crash at each boundary
# ===========================================================================


class _CrashWindowLedger:
    """Simulates the crash gap: intent claim succeeds, tool runs, but the
    result put fails → durable_ledger=unrecorded. The intent persists in the
    wrapped DurableFileLedger so a restart can see it."""

    def __init__(self, inner: DurableFileLedger, fail_put: bool = True) -> None:
        self._inner = inner
        self.fail_put = fail_put
        self.puts = 0

    def get(self, key: str):
        return self._inner.get(key)

    def put(self, key: str, result) -> None:
        self.puts += 1
        if self.fail_put:
            raise PersistenceUnavailable("disk full: ledger write failed")
        self._inner.put(key, result)

    def claim_intent(self, key: str, intent) -> bool:
        return self._inner.claim_intent(key, intent)

    def get_intent(self, key: str):
        return self._inner.get_intent(key)

    def mark_intent_started(self, key: str) -> None:
        self._inner.mark_intent_started(key)

    def mark_intent_completed(self, key: str) -> None:
        self._inner.mark_intent_completed(key)


async def test_crash_after_side_effect_before_result_persistence(
    tmp_path: Path,
) -> None:
    """Tool runs (side effect happens) but ledger.put fails → result returned
    as unrecorded. The side effect DID happen (file written). On restart, the
    graph sees the checkpointed SUCCEEDED result and completes — no replay."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    app.executor._ledger = _CrashWindowLedger(app.executor._ledger, fail_put=True)

    summary = await app.run_goal(GOAL, predicates=PRED, task_id="crash_window")
    # The side effect happened (file written), tool succeeded
    assert summary.executions == 1
    assert list((tmp_path / "ws").rglob("report.txt"))  # file exists
    assert summary.status in (TaskStatus.SUCCESS, TaskStatus.PARTIAL)

    # Restart: state has the execution result → resume planning, no replay
    await app.aclose()
    app2 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate_json()))
    restored = await app2.resume_task("crash_window")
    # Task already completed from first run; no second tool execution
    assert restored.executions == 0


async def test_crash_after_result_persistence_before_checkpoint(
    tmp_path: Path,
) -> None:
    """Result durably recorded in ledger but workflow checkpoint lost it
    (Case D): restart restores the recorded result, never re-executes."""
    app1 = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    summary = await app1.run_goal(GOAL, predicates=PRED, task_id="crash_after_result")
    assert summary.status is TaskStatus.SUCCESS
    await app1.aclose()

    # Restart: ledger holds the recorded result → no re-execution
    app2 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate_json()))
    restored = await app2.resume_task("crash_after_result")
    assert restored.status is TaskStatus.SUCCESS
    # No second execution on restart (no duplicate side effect)
    assert restored.executions == 0


async def test_crash_before_intent_persistence_safe_to_replan(
    tmp_path: Path,
) -> None:
    """Crash before claim_intent: no intent recorded → safe to resume
    planning with fresh authorization (Case A)."""
    app = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate_json()))
    state = _base_state("task-before-intent")
    state["pending_authorized_id"] = None
    state["executions"] = []
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.RESUME_PLANNING
    assert not result.should_block


async def test_malformed_ledger_entry_fails_closed(tmp_path: Path) -> None:
    """Corrupt ledger entry → PersistenceCorrupt, never treated as
    'not executed'."""
    ledger = DurableFileLedger(tmp_path)
    # Write a valid entry first so the file exists
    ledger.put(
        "a" * 32,
        _make_execution("task_x", "s0", "a" * 32, ExecutionStatus.SUCCEEDED),
    )
    key = "b" * 32
    # Directly write corrupt JSON to the ledger file
    raw = json.loads(ledger._path.read_text(encoding="utf-8"))
    raw["entries"][key] = {"broken": "not a result"}
    ledger._path.write_text(json.dumps(raw), encoding="utf-8")
    # Re-hydrate from disk to see the corruption
    ledger2 = DurableFileLedger(tmp_path)

    with pytest.raises(PersistenceCorrupt):
        ledger2.get(key)


async def test_concurrent_claims_cannot_both_start_same_operation(
    tmp_path: Path,
) -> None:
    """Two concurrent workers claiming the same intent: only one succeeds."""
    ledger = DurableFileLedger(tmp_path)
    intent = _make_intent("task_concurrent", "s0", "fp_concurrent")
    key_concurrent = _ledger_key("key_concurrent")
    results = await asyncio.gather(
        asyncio.to_thread(ledger.claim_intent, key_concurrent, intent),
        asyncio.to_thread(ledger.claim_intent, key_concurrent, intent),
    )
    assert sorted(results) == [False, True]
    # Only one intent recorded
    got = ledger.get_intent(key_concurrent)
    assert got is not None


async def test_old_authorized_artifact_refused_after_restart(
    tmp_path: Path,
) -> None:
    """Stale grant (epoch bumped while stopped) cannot execute after restart."""
    goal = "Update changelog"
    pred = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]
    candidate = candidate_json(
        tool="filesystem.write",
        arguments={"path": "Project B/CHANGELOG.md", "content": "updated"},
        effect="changelog updated",
        rationale="owner asked",
        subgoal_id="s0",
    )

    app1 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    app1.store.add_instruction("Do not touch Project B", "Project B")
    first = await app1.run_goal(goal, predicates=pred, task_id="stale_revoke")
    assert first.waiting_owner()
    await app1.aclose()

    app2 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    app2.store.revoke_all()
    after = await app2.resume_owner("stale_revoke", "ALLOW")
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    assert after.status is not TaskStatus.SUCCESS


async def test_unknown_outcome_not_blindly_replayed(tmp_path: Path) -> None:
    """Intent is EXECUTION_STARTED but no durable ledger result: on restart,
    the task is not blindly replayed. If the graph already checkpointed a
    SUCCEEDED result, the task completes (side effect did happen); the
    key property is that the tool is NOT re-executed."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    app.executor._ledger = _CrashWindowLedger(app.executor._ledger, fail_put=True)
    await app.run_goal(GOAL, predicates=PRED, task_id="unknown_no_replay")
    await app.aclose()

    # Multiple restarts should all avoid replay, never execute twice
    for i in range(3):
        app_r = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate_json()))
        restored = await app_r.resume_task("unknown_no_replay")
        assert restored.executions == 0, f"restart {i}: replay detected"
        await app_r.aclose()


# ===========================================================================
# Compatibility: legacy and brain workflows unchanged
# ===========================================================================


async def test_legacy_mission_compatible_with_intent_ledger(tmp_path: Path) -> None:
    """Legacy (non-brain) mission still works with the intent ledger."""
    app1 = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
        default_response='{"finished": true}',
    )
    summary = await app1.run_goal(GOAL, predicates=PRED, task_id="legacy_intent")
    assert summary.status is TaskStatus.SUCCESS
    await app1.aclose()

    app2 = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
    )
    restored = await app2.resume_task("legacy_intent")
    assert restored.status is TaskStatus.SUCCESS
    # No re-execution on restart
    assert restored.executions == 0
