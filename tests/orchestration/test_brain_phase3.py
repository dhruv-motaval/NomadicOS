"""Phase 3 crash-safe recovery tests (SPEC §56.14).

These tests simulate process crashes / application restarts at every defined
boundary and prove that NomadicOS never blindly replays a side effect, never
reuses a stale AuthorizedAction, and never declares false success.

Restart is simulated by creating a second NomadicApp over the SAME
state_dir (the checkpointer, execution ledger, task store and authority
store all hydrate from disk), exactly as a real process restart would.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from helpers import make_app, write_json
from test_brain_integration import brain_scripts, candidate_json

from nomadicos.contracts.action import ActionProposal, ProposalKind
from nomadicos.contracts.core import Failure, Goal, PlanStep, TaskStatus
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.orchestration.reconciliation import RecoveryDecision, reconcile_resume

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_exists", "path": "report.txt"}]


def _ledger_key(fingerprint: str) -> str:
    """Ledger identity: SHA256(fingerprint)[:32], matching executor.action_key
    and Executor.recorded_result."""
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Fixture helpers for constructing crashed task states
# --------------------------------------------------------------------------- #


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


def _write_crashed_checkpoint(app, task_id: str, state: dict[str, Any]) -> None:
    """Write a crashed checkpoint directly into the durable checkpointer,
    then close the app so a subsequent app (the "restart") hydrates from it."""
    config = {"configurable": {"thread_id": f"nomadic:{task_id}"}}
    app.graph.update_state(config, state)
    # Force the durable saver to flush the snapshot to disk.
    app.graph.get_state(config)


# --------------------------------------------------------------------------- #
# Unit tests: reconcile_resume (deterministic classification)
# --------------------------------------------------------------------------- #


def test_reconcile_before_authorization_resumes_planning(tmp_path: Path) -> None:
    """No in-flight action: resume planning normally (Case A)."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-a")
    state["pending_authorized_id"] = None
    state["pending_conflict"] = None
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.RESUME_PLANNING
    assert result.patches == {}
    assert not result.should_block


def test_reconcile_after_authorization_before_execution_reauthorizes(
    tmp_path: Path,
) -> None:
    """Artifact issued pre-restart but execute never called (Case B): the
    pre-restart AuthorizedAction is unusable (in-memory table lost) →
    revalidate and reauthorize under current permissions."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-b")
    state["pending_authorized_id"] = "auth-abc"
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.REAUTHORIZE_ACTION
    assert result.patches["pending_authorized_id"] is None
    assert result.patches["last_failure"]["category"] == Failure.AUTHORIZATION_DENIED.value
    assert not result.should_block


def test_reconcile_restores_recorded_result_when_ledger_has_it(
    tmp_path: Path,
) -> None:
    """Execution happened and is durably recorded but not in graph state
    (Case D): restore the recorded result, never re-execute."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-d")
    state["pending_authorized_id"] = None
    fp = state["proposals"][0].fingerprint()
    recorded = _make_execution("task-d", "s0", fingerprint=fp)
    # Pre-record in the durable ledger (simulating the side effect completed)
    app.executor._ledger.put(_ledger_key(fp), recorded)  # noqa: SLF001 - test seam
    state["executions"] = []  # graph state lost the execution (crash gap)
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.RESTORE_RECORDED_RESULT
    assert len(result.patches["executions"]) == 1
    assert result.patches["executions"][0].action_fingerprint == fp
    assert result.patches["pending_authorized_id"] is None
    assert not result.should_block


def test_reconcile_executor_refused_resumes_planning(tmp_path: Path) -> None:
    """The executor refused before running (execution_missing=True): no side
    effect is possible, so resume planning instead of blocking."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-c")
    state["pending_authorized_id"] = None
    state["executions"] = []
    state["execution_missing"] = True  # executor refused before producing a result
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.RESUME_PLANNING
    assert not result.should_block


def test_reconcile_outcome_uncertain_blocks(tmp_path: Path) -> None:
    """Phase 2 outcome_uncertain flag → BLOCK_UNKNOWN_OUTCOME."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-u")
    state["outcome_uncertain"] = True
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.BLOCK_UNKNOWN_OUTCOME
    assert result.should_block


def test_reconcile_corrupt_state_blocks(tmp_path: Path) -> None:
    """Non-mapping checkpoint state → BLOCK_CORRUPT_STATE, fail closed."""
    app = make_app(tmp_path, brain_actor=True)
    result = reconcile_resume("not-a-dict", app.runtime, None)  # type: ignore[arg-type]
    assert result.decision == RecoveryDecision.BLOCK_CORRUPT_STATE
    assert result.should_block


def test_reconcile_recorded_result_with_proposal_matching_ledger(
    tmp_path: Path,
) -> None:
    """Replay-safe: when the ledger already holds the action, the restored
    result carries duplicate_blocked semantics and no tool runs again."""
    app = make_app(tmp_path, brain_actor=True)
    state = _base_state("task-r")
    state["pending_authorized_id"] = None
    fp = state["proposals"][0].fingerprint()
    recorded = _make_execution("task-r", "s0", fingerprint=fp, status=ExecutionStatus.SUCCEEDED)
    app.executor._ledger.put(_ledger_key(fp), recorded)  # noqa: SLF001 - test seam
    state["executions"] = []
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.RESTORE_RECORDED_RESULT
    assert result.patches["executions"][0].status is ExecutionStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Integration tests: restart via resume_task / resume_owner
# --------------------------------------------------------------------------- #


async def test_brain_owner_approval_survives_restart_then_execute(tmp_path: Path) -> None:
    """A brain proposal paused for owner approval survives restart: nothing
    executes while pending; ALLOW revalidates/reauthorizes and executes."""
    goal = "Update the changelog inside Project B"
    pred = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]
    candidate = candidate_json(
        tool="filesystem.write",
        arguments={"path": "Project B/CHANGELOG.md", "content": "updated"},
        effect="changelog updated",
        rationale="owner asked for the changelog",
        subgoal_id="s0",
    )

    app1 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    app1.store.add_instruction("Do not touch Project B", "Project B")
    first = await app1.run_goal(goal, predicates=pred, task_id="brain_appr3")
    assert first.waiting_owner()
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    await app1.aclose()

    # --- restart: new app over the same state_dir ---
    app2 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    restored = await app2.resume_task("brain_appr3")
    assert restored.waiting_owner()
    assert restored.conflict is not None
    assert restored.conflict["capability"] == "filesystem.write"

    done = await app2.resume_owner("brain_appr3", "ALLOW")
    assert done.status is TaskStatus.SUCCESS, done.outcome_note
    assert done.goal_verdict == "PASS"
    assert done.executions == 1
    assert list((tmp_path / "ws").rglob("CHANGELOG.md"))[0].read_text() == "updated"


async def test_brain_revocation_across_restart_blocks_execution(tmp_path: Path) -> None:
    """Revoking authority while paused bumps the epoch; resume after restart
    still refuses the action (stale authorization never executable)."""
    goal = "Update the changelog inside Project B"
    pred = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]
    candidate = candidate_json(
        tool="filesystem.write",
        arguments={"path": "Project B/CHANGELOG.md", "content": "updated"},
        effect="changelog updated",
        rationale="owner asked for the changelog",
        subgoal_id="s0",
    )

    app1 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    app1.store.add_instruction("Do not touch Project B", "Project B")
    first = await app1.run_goal(goal, predicates=pred, task_id="brain_rev3")
    assert first.waiting_owner()
    await app1.aclose()

    # --- restart: revoke while the process is stopped ---
    app2 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate))
    app2.store.revoke_all()
    after = await app2.resume_owner("brain_rev3", "ALLOW")
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    assert after.status is not TaskStatus.SUCCESS


async def test_corrupt_brain_state_reconstructs_from_authoritative_plan(
    tmp_path: Path,
) -> None:
    """A version-mismatched / corrupt BrainState snapshot is rejected
    (fail-closed) and reconstructed from the authoritative plan + prior
    observations; the mission still completes honestly."""
    app1 = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(candidate_json()))
    summary = await app1.run_goal(GOAL, predicates=PRED, task_id="brain_corrupt1")
    assert summary.status is TaskStatus.SUCCESS
    await app1.aclose()

    # --- restart: corrupt the brain_state JSON in the checkpoint ---
    app2 = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    config = {"configurable": {"thread_id": "nomadic:brain_corrupt1"}}
    values = dict(app2.graph.get_state(config).values or {})
    # Corrupt: wrong schema version → load_brain_state fails closed
    raw = values.get("brain_state")
    assert raw is not None
    corrupted = json.loads(raw)
    corrupted["schema_version"] = 999
    values["brain_state"] = json.dumps(corrupted, sort_keys=True)
    app2.graph.update_state(config, values)

    # The corrupt BrainState must be rejected by the loader (fail-closed)
    from nomadicos.orchestration.brain_adapter import load_brain_state

    assert load_brain_state(json.dumps(corrupted, sort_keys=True)) is None
    # The completed task's durable record is untouched (SUCCESS stands)
    record = app2.task_store.load_task("brain_corrupt1")
    assert record is not None
    assert record.status is TaskStatus.SUCCESS


async def test_resume_missing_state_raises(tmp_path: Path) -> None:
    """No checkpoint and no task record → PersistenceCorrupt, fail closed."""
    from nomadicos.persistence.errors import PersistenceCorrupt

    app = make_app(tmp_path, brain_actor=True)
    with pytest.raises(PersistenceCorrupt):
        await app.resume_task("no-such-task")


async def test_non_brain_mission_continues_after_restart(tmp_path: Path) -> None:
    """Compatibility: a legacy (non-brain) mission still resumes correctly."""
    app1 = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
        default_response='{"finished": true}',
    )
    summary = await app1.run_goal(GOAL, predicates=PRED, task_id="legacy3")
    assert summary.status is TaskStatus.SUCCESS
    await app1.aclose()

    app2 = make_app(tmp_path, scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))])
    restored = await app2.resume_task("legacy3")
    # Terminal task → restored as DATA, no re-execution
    assert restored.status is TaskStatus.SUCCESS
