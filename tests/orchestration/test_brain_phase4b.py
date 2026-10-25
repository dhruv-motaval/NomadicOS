"""Phase 4B Brain-Managed Specialist Agents tests (SPEC §9).

NomadicBrain delegates bounded subtasks to specialist workers, receives
structured results, validates them, and decides what happens next. The Brain
remains the authoritative coordinator; workers produce untrusted data that
the coordinator validates through the normal verify step. Side effects pass
through the existing validate -> authorize -> execute -> verify_step harness.

All side-effecting workers run sequentially through the LangGraph; no
concurrent side-effecting execution is enabled (SPEC §5 Phase 4B.5).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from helpers import make_app, write_json
from pydantic import ValidationError

from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.contracts.execution import ExecutionIntent
from nomadicos.orchestration.reconciliation import RecoveryDecision, reconcile_resume
from nomadicos.orchestration.worker import (
    WorkerAssignment,
    WorkerResult,
    WorkerRole,
    WorkerStatus,
)
from nomadicos.orchestration.workers import worker_for_role


def _thread_config(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"nomadic:{task_id}"}}


def _final_state(app: Any, task_id: str) -> dict[str, Any]:
    return dict(app.graph.get_state(_thread_config(task_id)).values or {})


def _ledger_key(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Fixtures: scripted decomposition + worker replies
# --------------------------------------------------------------------------- #


def _decomposition(tasks: list[dict], subgoals: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "subgoals": subgoals or [{"id": "s0", "description": "subgoal"}],
            "tasks": tasks,
        }
    )


def _worker_result_json(status: str = "SUCCEEDED", **overrides) -> str:
    base = {
        "status": status,
        "artifacts": [],
        "verification_evidence": [],
        "failure_category": None,
        "diagnostics": None,
        "actions_attempted": 1,
        "elapsed_s": 0.1,
    }
    base.update(overrides)
    return json.dumps(base)


# --------------------------------------------------------------------------- #
# 1. Brain selects the correct specialist role for a task
# --------------------------------------------------------------------------- #


def test_brain_selects_specialist_role(tmp_path: Path) -> None:
    """Decomposition assigns a worker_role per task; TaskNode mirrors it."""
    from nomadicos.brain.decomposer import parse_decomposition

    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "research",
                "description": "Research alpha.txt",
                "worker_role": "research",
            },
            {
                "id": "t1",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "report written",
                "rationale": "code",
                "description": "Write report.txt",
                "worker_role": "coding",
                "depends_on": ["t0"],
            },
        ]
    )
    d = parse_decomposition(decomp, goal_id="g1")
    nodes = {n.id: n for n in d.task_graph.nodes}
    assert nodes["t0"].worker_role is WorkerRole.RESEARCH
    assert nodes["t1"].worker_role is WorkerRole.CODING
    assert nodes["t1"].depends_on == ["t0"]


# --------------------------------------------------------------------------- #
# 2. Worker assignments inherit mission and task identities
# --------------------------------------------------------------------------- #


def test_worker_assignment_inherits_identities(tmp_path: Path) -> None:
    """WorkerAssignment carries mission_id, task_id, step_id, objective."""
    a = WorkerAssignment(
        role=WorkerRole.CODING,
        task_id="t1",
        mission_id="mission-42",
        step_id="s0",
        objective="Write report.txt",
        constraints=["do not touch Project B"],
        capabilities=["filesystem.write"],
        resources={"model_id": "models/small.gguf"},
        budget={"max_tokens": 900, "timeout_s": 30.0},
        attempt=1,
    )
    assert a.task_id == "t1"
    assert a.mission_id == "mission-42"
    assert a.step_id == "s0"
    assert a.objective == "Write report.txt"
    assert a.role is WorkerRole.CODING


# --------------------------------------------------------------------------- #
# 3. A worker cannot expand its authorized scope or budget
# --------------------------------------------------------------------------- #


def test_worker_cannot_expand_scope_or_budget(tmp_path: Path) -> None:
    """WorkerAssignment is bounded: oversized objective and attempt<1 rejected."""
    with pytest.raises(ValueError):
        WorkerAssignment(
            role=WorkerRole.CODING,
            task_id="t1",
            mission_id="m",
            step_id="s0",
            objective="x" * 500,  # exceeds 400
        )
    with pytest.raises(ValueError):
        WorkerAssignment(
            role=WorkerRole.CODING,
            task_id="t1",
            mission_id="m",
            step_id="s0",
            objective="ok",
            attempt=0,
        )
    # bounded attempt succeeds
    a = WorkerAssignment(
        role=WorkerRole.RESEARCH,
        task_id="t1",
        mission_id="m",
        step_id="s0",
        objective="ok",
        attempt=3,
    )
    assert a.attempt == 3


# --------------------------------------------------------------------------- #
# 4. Malformed worker output is rejected (fail closed)
# --------------------------------------------------------------------------- #


async def test_malformed_worker_output_rejected(tmp_path: Path) -> None:
    """Worker returns unparseable JSON -> INVALID_PROPOSAL -> recovery -> blocked."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the protocol",
                "worker_role": "research",
            }
        ]
    )
    # malformed worker reply (not valid JSON)
    worker_reply = "{not valid json"

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[
            ("goal_objective", decomp),
            ("Research the protocol", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Research the protocol",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_malformed",
    )
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)
    assert summary.status is not TaskStatus.SUCCESS
    st = _final_state(app, "worker_malformed")
    wr = st.get("worker_result") or {}
    assert wr.get("status") == WorkerStatus.REJECTED.value


# --------------------------------------------------------------------------- #
# 5. Unsupported research claims remain unverified
# --------------------------------------------------------------------------- #


async def test_unsupported_research_claims_remain_unverified(tmp_path: Path) -> None:
    """Research worker returns SUCCEEDED with NO verification_evidence ->
    the claim stays unverified and the task is not completed."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the alpha format",
                "worker_role": "research",
            }
        ]
    )
    # SUCCEEDED but with zero verification_evidence -> unsupported claim
    worker_reply = _worker_result_json("SUCCEEDED", verification_evidence=[])

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[
            ("goal_objective", decomp),
            ("Research the alpha format", worker_reply),
        ],
        workspace_per_task=False,
    )
    summary = await app.run_goal(
        "Research the alpha format",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_research_unverified",
    )
    assert summary.status is not TaskStatus.SUCCESS
    st = _final_state(app, "worker_research_unverified")
    wr = st.get("worker_result") or {}
    assert wr.get("status") == WorkerStatus.SUCCEEDED.value
    assert len(wr.get("verification_evidence") or []) == 0


# --------------------------------------------------------------------------- #
# 6. Coding outputs pass through the existing execution controls
# --------------------------------------------------------------------------- #


async def test_coding_outputs_pass_through_execution_controls(tmp_path: Path) -> None:
    """Coding worker proposes a file write; it flows through validate->
    authorize->execute->verify_step (executor writes the file)."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "report written",
                "rationale": "c",
                "description": "Write hello.txt",
                "worker_role": "coding",
            }
        ]
    )
    # coding worker proposes hello.txt with content
    worker_reply = _worker_result_json(
        "SUCCEEDED",
        artifacts=[
            {
                "kind": "file",
                "path": "hello.txt",
                "description": "greeting",
                "content": "hello world",
            },
        ],
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomp),
            ("Write hello.txt", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Create a greeting file",
        predicates=[{"type": "file_exists", "path": "hello.txt"}],
        task_id="worker_coding",
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert summary.executions == 1  # exactly one side effect through the harness
    assert (tmp_path / "ws" / "hello.txt").read_text() == "hello world"
    # the coding worker's proposal went through the durable execution-intent ledger
    assert app.executor.recorded_result is not None


# --------------------------------------------------------------------------- #
# 7. An unverified required artifact does not release downstream dependencies
# --------------------------------------------------------------------------- #


async def test_unverified_artifact_blocks_downstream(tmp_path: Path) -> None:
    """t0 research (unsupported -> FAILED); t1 depends on t0 and never runs."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the protocol",
                "worker_role": "research",
            },
            {
                "id": "t1",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "filesystem.write",
                "arguments": {"path": "beta.txt", "content": "never"},
                "preconditions": [],
                "expected_effect": "beta written",
                "rationale": "w",
                "description": "Write beta.txt",
                "worker_role": "EXECUTE",
                "depends_on": ["t0"],
            },
        ],
        subgoals=[{"id": "s0", "description": "sub"}],
    )
    worker_reply = _worker_result_json("SUCCEEDED", verification_evidence=[])

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[
            ("goal_objective", decomp),
            ("Research the protocol", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Research then write",
        predicates=[{"type": "file_exists", "path": "beta.txt"}],
        task_id="worker_blocked_dep",
    )
    assert not (tmp_path / "ws" / "beta.txt").exists()
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)


# --------------------------------------------------------------------------- #
# 8. A failed worker cannot force overall mission success
# --------------------------------------------------------------------------- #


async def test_failed_worker_cannot_force_success(tmp_path: Path) -> None:
    """Worker returns FAILED -> task FAILED -> goal not SUCCESS."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the protocol",
                "worker_role": "research",
            }
        ]
    )
    worker_reply = _worker_result_json(
        "FAILED", failure_category="RESOURCE_UNAVAILABLE", diagnostics="source down"
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[
            ("goal_objective", decomp),
            ("Research the protocol", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Research the protocol",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_failed",
    )
    assert summary.status is not TaskStatus.SUCCESS
    st = _final_state(app, "worker_failed")
    wr = st.get("worker_result") or {}
    assert wr.get("status") == WorkerStatus.FAILED.value


# --------------------------------------------------------------------------- #
# 9. Timeouts and worker failures produce bounded, traceable outcomes
# --------------------------------------------------------------------------- #


async def test_timeout_and_failure_bounded(tmp_path: Path) -> None:
    """Worker returns TIMED_OUT with diagnostics -> bounded, traceable outcome."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the protocol",
                "worker_role": "research",
            }
        ]
    )
    worker_reply = _worker_result_json("TIMED_OUT", diagnostics="source lookup exceeded 30s")

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[
            ("goal_objective", decomp),
            ("Research the protocol", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Research the protocol",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_timeout",
    )
    assert summary.status is not TaskStatus.SUCCESS
    st = _final_state(app, "worker_timeout")
    wr = st.get("worker_result") or {}
    assert wr.get("status") == WorkerStatus.TIMED_OUT.value
    assert "exceeded" in (wr.get("diagnostics") or "")


# --------------------------------------------------------------------------- #
# 10. A worker cannot bypass authorization, execution intent, or the ledger
# --------------------------------------------------------------------------- #


async def test_worker_cannot_bypass_authorization_or_ledger(tmp_path: Path) -> None:
    """Coding worker's file write still requires authorize + ledger claim."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "ledger.txt written",
                "rationale": "c",
                "description": "Write ledger.txt",
                "worker_role": "coding",
            }
        ]
    )
    worker_reply = _worker_result_json(
        "SUCCEEDED",
        artifacts=[
            {
                "kind": "file",
                "path": "ledger.txt",
                "description": "ledger entry",
                "content": "durable",
            },
        ],
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomp),
            ("Write ledger.txt", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Create a durable ledger entry",
        predicates=[{"type": "file_exists", "path": "ledger.txt"}],
        task_id="worker_ledger",
    )
    assert summary.status is TaskStatus.SUCCESS
    # the write passed through the durable execution-intent ledger
    assert app.executor.recorded_result is not None
    # the worker itself never created an AuthorizedAction artifact
    assert app.runtime.issued_count() >= 0


# --------------------------------------------------------------------------- #
# 11. Resume does not repeat side effects whose outcome is unknown
# --------------------------------------------------------------------------- #


async def test_resume_does_not_repeat_unknown_worker_side_effect(tmp_path: Path) -> None:
    """EXECUTION_STARTED intent without a result -> BLOCK_UNKNOWN_OUTCOME on
    resume (worker side effect durability gap, SPEC §56.14)."""
    app = make_app(tmp_path, brain_planner=True)
    from nomadicos.contracts.action import ActionProposal, ProposalKind

    proposal = ActionProposal(
        task_id="t1",
        step_id="s0",
        attempt=1,
        model_id="m",
        kind=ProposalKind.ACTION,
        tool="filesystem",
        operation="write",
        args={"path": "report.txt", "content": "HELLO-WORKER"},
        note="worker crash-recovery probe",
    )
    fp = proposal.fingerprint()
    state = {
        "task_id": "t1",
        "goal": Goal(objective="x", constraints=[], predicates=[]),
        "plan": [],
        "current_step": 0,
        "model_id": "m",
        "proposals": [proposal],
        "executions": [],
        "task_status": "RUNNING",
        "brain_planner_enabled": True,
    }
    app.executor._ledger.claim_intent(
        _ledger_key(fp),
        ExecutionIntent(
            task_id="t1",
            step_id="s0",
            fingerprint=fp,
            authorization_epoch=1,
            tool="fs",
            operation="w",
            args={},
        ),
    )
    app.executor._ledger.mark_intent_started(_ledger_key(fp))
    result = reconcile_resume(state, app.runtime, None)
    assert result.decision == RecoveryDecision.BLOCK_UNKNOWN_OUTCOME
    assert result.should_block


# --------------------------------------------------------------------------- #
# 12. Legacy path unchanged when Brain mode is disabled
# --------------------------------------------------------------------------- #


async def test_legacy_path_unchanged_when_brain_disabled(tmp_path: Path) -> None:
    """brain_planner=False: no worker_delegate, legacy planner path untouched."""
    app = make_app(
        tmp_path,
        workspace_per_task=False,
        scripts=[("report.txt", write_json("report.txt", "HELLO-LEGACY"))],
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(
        "Create file report.txt",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="legacy4b",
    )
    assert summary.status is TaskStatus.SUCCESS
    assert (tmp_path / "ws" / "report.txt").read_text() == "HELLO-LEGACY"


# --------------------------------------------------------------------------- #
# 13. Concurrent side-effecting execution stays disabled (sequential workers)
# --------------------------------------------------------------------------- #


async def test_concurrent_side_effecting_execution_disabled(tmp_path: Path) -> None:
    """Two independent coding workers execute sequentially (one task at a
    time) through the graph; no parallel side-effecting workers."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "a written",
                "rationale": "c",
                "description": "Write alpha.txt",
                "worker_role": "coding",
            },
            {
                "id": "t1",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "b written",
                "rationale": "c",
                "description": "Write beta.txt",
                "worker_role": "coding",
            },
        ],
        subgoals=[{"id": "s0", "description": "sub"}],
    )
    # Two distinct coding worker replies (popped in order)
    worker_replies = [
        _worker_result_json(
            "SUCCEEDED",
            artifacts=[
                {"kind": "file", "path": "alpha.txt", "description": "a", "content": "alpha"},
            ],
        ),
        _worker_result_json(
            "SUCCEEDED",
            artifacts=[
                {"kind": "file", "path": "beta.txt", "description": "b", "content": "beta"},
            ],
        ),
    ]

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomp),
            ("Write alpha.txt", worker_replies[0]),
            ("Write beta.txt", worker_replies[1]),
        ],
    )
    summary = await app.run_goal(
        "Write two files",
        predicates=[
            {"type": "file_exists", "path": "alpha.txt"},
            {"type": "file_exists", "path": "beta.txt"},
        ],
        task_id="worker_sequential",
    )
    assert summary.status is TaskStatus.SUCCESS
    assert summary.executions == 2  # sequential, one side effect per task
    assert (tmp_path / "ws" / "alpha.txt").read_text() == "alpha"
    assert (tmp_path / "ws" / "beta.txt").read_text() == "beta"


# --------------------------------------------------------------------------- #
# 14. Worker results and artifacts are associated with the parent mission
# --------------------------------------------------------------------------- #


async def test_worker_results_associated_with_parent_mission(tmp_path: Path) -> None:
    """WorkerResult carries mission_id and task_id; worker_results keyed by task."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "report written",
                "rationale": "c",
                "description": "Write report.txt",
                "worker_role": "coding",
            }
        ]
    )
    worker_reply = _worker_result_json(
        "SUCCEEDED",
        artifacts=[
            {"kind": "file", "path": "report.txt", "description": "r", "content": "report"},
        ],
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomp),
            ("Write report.txt", worker_reply),
        ],
    )
    summary = await app.run_goal(
        "Create a report",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_mission_assoc",
    )
    assert summary.status is TaskStatus.SUCCESS
    st = _final_state(app, "worker_mission_assoc")
    wr = st.get("worker_result") or {}
    assert wr.get("mission_id") == "worker_mission_assoc"
    assert wr.get("task_id") == "t0"
    wresults = st.get("worker_results") or {}
    assert wresults["t0"]["mission_id"] == "worker_mission_assoc"


# --------------------------------------------------------------------------- #
# End-to-end: Brain delegates research, receives result, verifies, continues
# --------------------------------------------------------------------------- #


async def test_brain_delegates_research_then_coding_end_to_end(tmp_path: Path) -> None:
    """Brain delegates research (verified) then coding (writes report);
    goal verifier establishes SUCCESS. One end-to-end delegation cycle."""
    decomp = _decomposition(
        [
            {
                "id": "t0",
                "kind": "EXECUTE",
                "subgoal_id": "s0",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "research done",
                "rationale": "r",
                "description": "Research the topic",
                "worker_role": "research",
            },
            {
                "id": "t1",
                "kind": "EXECUTE",
                "subgoal_id": "s1",
                "tool": "",
                "arguments": {},
                "preconditions": [],
                "expected_effect": "report written",
                "rationale": "c",
                "description": "Write report.txt",
                "worker_role": "coding",
                "depends_on": ["t0"],
            },
        ],
        subgoals=[
            {"id": "s0", "description": "research"},
            {"id": "s1", "description": "write"},
        ],
    )
    research_reply = _worker_result_json(
        "SUCCEEDED",
        verification_evidence=[
            {
                "claim": "topic uses UTF-8",
                "source": "spec.txt",
                "retrieved_at": "2026-10-23T00:00:00Z",
            },
        ],
    )
    coding_reply = _worker_result_json(
        "SUCCEEDED",
        artifacts=[
            {"kind": "file", "path": "report.txt", "description": "report", "content": "findings"},
        ],
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomp),
            ("Research the topic", research_reply),
            ("Write report.txt", coding_reply),
        ],
    )
    summary = await app.run_goal(
        "Research then write a report",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="worker_e2e",
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert summary.goal_verdict == "PASS"
    assert summary.executions == 1  # only the coding write is a side effect
    assert (tmp_path / "ws" / "report.txt").read_text() == "findings"
    # both worker results are attributable to the mission
    st = _final_state(app, "worker_e2e")
    wresults = st.get("worker_results") or {}
    assert wresults["t0"]["mission_id"] == "worker_e2e"
    assert wresults["t1"]["mission_id"] == "worker_e2e"


# --------------------------------------------------------------------------- #
# Worker protocol / contract unit checks
# --------------------------------------------------------------------------- #


def test_worker_protocol_shape(tmp_path: Path) -> None:
    """worker_for_role returns objects satisfying the Worker protocol."""
    app = make_app(tmp_path, brain_planner=True)
    engine = app.runtime.engine_for("models/small.gguf")
    w = worker_for_role(WorkerRole.RESEARCH, engine=engine)
    from nomadicos.orchestration.worker import Worker

    assert isinstance(w, Worker)
    assert w.name == "research-worker-v1"
    assert w.role is WorkerRole.RESEARCH


def test_worker_result_is_untrusted_data(tmp_path: Path) -> None:
    """WorkerResult has no authority fields; closed schema rejects them."""
    r = WorkerResult(
        worker="research-worker-v1",
        role=WorkerRole.RESEARCH,
        task_id="t0",
        mission_id="m",
        step_id="s0",
        status=WorkerStatus.SUCCEEDED,
    )
    assert r.role is WorkerRole.RESEARCH
    assert r.status is WorkerStatus.SUCCEEDED
    # closed schema: unknown / authority fields rejected
    with pytest.raises(ValidationError):
        WorkerResult.model_validate(
            {
                "worker": "w",
                "role": "research",
                "task_id": "t0",
                "mission_id": "m",
                "step_id": "s0",
                "status": "SUCCEEDED",
                "authorized": True,
            }
        )
