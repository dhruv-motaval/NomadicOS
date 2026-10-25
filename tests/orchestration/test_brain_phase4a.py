"""Phase 4A Brain-led mission planning tests (SPEC §13, §27-29).

Tests NomadicBrain as the primary mission planner: decomposition, validation,
dependency-aware scheduling, and end-to-end execution through the existing
harness. All side effects go through validate → authorize → execute → observe
→ verify_step. Only the authoritative goal verifier establishes SUCCESS.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from helpers import make_app, write_json
from test_brain_integration import brain_scripts, candidate_json

from nomadicos.contracts.action import ActionProposal, ProposalKind
from nomadicos.contracts.core import TaskStatus
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.orchestration.brain_planner import (
    init_task_status_map,
    mission_plan_to_steps,
    next_eligible_task,
    task_eligible,
    validate_mission_plan,
)
from nomadicos.orchestration.reconciliation import RecoveryDecision, reconcile_resume

GOAL = "Create files alpha.txt and beta.txt with matching contents"
PRED = [
    {"type": "file_exists", "path": "alpha.txt"},
    {"type": "file_exists", "path": "beta.txt"},
]


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


# --------------------------------------------------------------------------- #
# Unit tests: plan validation and scheduling
# --------------------------------------------------------------------------- #


def test_validate_mission_plan_rejects_wrong_goal(tmp_path: Path) -> None:
    """Brain plan for a different goal is rejected."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create alpha.txt", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id="other-goal",
        nodes=[
            TaskNode(id="t0", description="write alpha", depends_on=[]),
        ],
    )
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    with pytest.raises(InvalidProposal):
        validate_mission_plan(graph, goal, registry)


def test_validate_mission_plan_rejects_self_dependency(tmp_path: Path) -> None:
    """A task depending on itself is rejected by TaskGraph.build()."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create files", constraints=[], predicates=[])
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    with pytest.raises((ValueError, TypeError)):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=[
                TaskNode(id="t0", description="write alpha", depends_on=["t0"]),  # self-dep
            ],
        )


def test_validate_mission_plan_rejects_unknown_dependency(tmp_path: Path) -> None:
    """A dependency on a non-existent task is rejected by TaskGraph.build()."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create files", constraints=[], predicates=[])
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    with pytest.raises((ValueError, TypeError)):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=[
                TaskNode(id="t0", description="write alpha", depends_on=["t99"]),  # unknown dep
            ],
        )


def test_validate_mission_plan_rejects_cycle(tmp_path: Path) -> None:
    """A cyclic dependency graph is rejected by TaskGraph.build()."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create files", constraints=[], predicates=[])
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    with pytest.raises((ValueError, TypeError)):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=[
                TaskNode(id="t0", description="write alpha", depends_on=["t1"]),
                TaskNode(id="t1", description="write beta", depends_on=["t0"]),  # cycle
            ],
        )


def test_validate_mission_plan_rejects_oversized(tmp_path: Path) -> None:
    """A plan exceeding MAX_NODES is rejected by TaskGraph.build()."""
    from nomadicos.brain.planner.task_graph import MAX_NODES, TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create files", constraints=[], predicates=[])
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    with pytest.raises((ValueError, TypeError)):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=[
                TaskNode(id=f"t{i}", description=f"step {i}", depends_on=[])
                for i in range(MAX_NODES + 1)
            ],
        )


def test_validate_mission_plan_accepts_valid_plan(tmp_path: Path) -> None:
    """A well-formed plan passes validation."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal
    from nomadicos.tools.base import ToolRegistry

    goal = Goal(objective="create files", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="t0", description="write alpha", depends_on=[]),
            TaskNode(id="t1", description="write beta", depends_on=["t0"]),
        ],
    )
    registry = ToolRegistry()
    registry.register(__import__("nomadicos.tools.filesystem", fromlist=["*"]).FilesystemTool())

    # Should not raise
    validate_mission_plan(graph, goal, registry)


# --------------------------------------------------------------------------- #
# Unit tests: scheduling
# --------------------------------------------------------------------------- #


def test_next_eligible_task_returns_ready_only(tmp_path: Path) -> None:
    """Only READY tasks are eligible; PENDING tasks with unmet deps are not."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
    from nomadicos.contracts.core import Goal

    goal = Goal(objective="x", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="t0", description="first", depends_on=[]),
            TaskNode(id="t1", description="second", depends_on=["t0"]),
        ],
    )
    status_map = init_task_status_map(graph)

    # Initially only t0 is READY
    assert next_eligible_task(status_map, [], graph) == "t0"

    # After t0 completes AND t1 is unlocked to READY (as advance_task does),
    # t1 becomes eligible
    status_map["t0"] = TaskNodeStatus.COMPLETED.value
    status_map["t1"] = TaskNodeStatus.READY.value  # advance_task unlocks it
    assert next_eligible_task(status_map, [], graph) == "t1"

    # After t1 completes, nothing is eligible
    status_map["t1"] = TaskNodeStatus.COMPLETED.value
    assert next_eligible_task(status_map, [], graph) is None


def test_task_eligible_blocks_on_failed_dependency(tmp_path: Path) -> None:
    """A task whose dependency FAILED is not eligible (BLOCKED propagation)."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
    from nomadicos.contracts.core import Goal

    goal = Goal(objective="x", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="t0", description="first", depends_on=[]),
            TaskNode(id="t1", description="second", depends_on=["t0"]),
        ],
    )
    status_map = init_task_status_map(graph)

    # t0 fails → t1 is BLOCKED, not eligible
    status_map["t0"] = TaskNodeStatus.FAILED.value
    status_map["t1"] = TaskNodeStatus.BLOCKED.value
    assert not task_eligible(status_map, "t1", graph)


def test_task_status_map_init_from_graph(tmp_path: Path) -> None:
    """init_task_status_map derives READY/PENDING from graph structure."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal

    goal = Goal(objective="x", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="t0", description="first", depends_on=[]),
            TaskNode(id="t1", description="second", depends_on=["t0"]),
        ],
    )
    status_map = init_task_status_map(graph)
    assert status_map["t0"] == "READY"
    assert status_map["t1"] == "PENDING"


def test_mission_plan_to_steps_topological_order(tmp_path: Path) -> None:
    """mission_plan_to_steps emits nodes in topological order."""
    from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
    from nomadicos.contracts.core import Goal

    goal = Goal(objective="x", constraints=[], predicates=[])
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="t1", description="second", depends_on=["t0"]),
            TaskNode(id="t0", description="first", depends_on=[]),
        ],
    )
    steps = mission_plan_to_steps(graph, goal.id)
    assert [s.id for s in steps] == ["t0", "t1"]


# --------------------------------------------------------------------------- #
# Unit tests: crash-gap recovery with intent (Phase 3.1)
# --------------------------------------------------------------------------- #


def test_reconcile_intent_started_blocks_unknown(tmp_path: Path) -> None:
    """EXECUTION_STARTED intent without a result → BLOCK_UNKNOWN_OUTCOME."""
    from nomadicos.contracts.core import Goal
    from nomadicos.contracts.execution import ExecutionIntent

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
        args={"path": "report.txt", "content": "HELLO-GRAPH"},
        note="crash-recovery probe",
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
# Integration tests: end-to-end brain-led mission
# --------------------------------------------------------------------------- #


async def test_brain_led_mission_end_to_end(tmp_path: Path) -> None:
    """Brain decomposes a goal into dependent tasks; each executes through
    the harness; the verifier establishes SUCCESS only when all predicates
    hold. No side effect is duplicated."""
    # Brain decomposition payload (what the decomposer model returns)
    decomposition_json = json.dumps(
        {
            "subgoals": [
                {"id": "s0", "description": "Write alpha.txt"},
                {"id": "s1", "description": "Write beta.txt"},
            ],
            "tasks": [
                {
                    "id": "t0",
                    "kind": "EXECUTE",
                    "subgoal_id": "s0",
                    "tool": "filesystem.write",
                    "arguments": {"path": "alpha.txt", "content": "hello"},
                    "preconditions": [],
                    "expected_effect": "alpha.txt exists",
                    "rationale": "write alpha",
                },
                {
                    "id": "t1",
                    "kind": "EXECUTE",
                    "subgoal_id": "s1",
                    "tool": "filesystem.write",
                    "arguments": {"path": "beta.txt", "content": "hello"},
                    "preconditions": [],
                    "expected_effect": "beta.txt exists",
                    "rationale": "write beta",
                    "depends_on": ["t0"],
                },
            ],
        }
    )
    # Per-task actor/monitor scripts
    monitor = json.dumps(
        {
            "verdicts": [
                {"candidate_id": "c1", "accepted": True, "reason": "ok"},
                {"candidate_id": "c2", "accepted": True, "reason": "ok"},
            ]
        }
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=False,
        scripts=[
            ("goal_objective", decomposition_json),
            ("CANDIDATES AND CONTEXT", monitor),
        ],
    )
    summary = await app.run_goal(GOAL, predicates=PRED, task_id="brain4a_e2e")

    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert summary.goal_verdict == "PASS"
    assert summary.executions == 2  # exactly 2 side effects, no duplicates

    # Both files exist with correct content
    assert (tmp_path / "ws" / "alpha.txt").read_text() == "hello"
    assert (tmp_path / "ws" / "beta.txt").read_text() == "hello"


async def test_brain_mission_failed_dependency_blocks_downstream(
    tmp_path: Path,
) -> None:
    """If task 0 fails, task 1 (dependent) is BLOCKED and never executes."""
    decomposition_json = json.dumps(
        {
            "subgoals": [
                {"id": "s0", "description": "Run failing command"},
                {"id": "s1", "description": "Write beta.txt"},
            ],
            "tasks": [
                {
                    "id": "t0",
                    "kind": "EXECUTE",
                    "subgoal_id": "s0",
                    "tool": "terminal.execute",
                    "arguments": {"command": "cmd", "args": ["/c", "exit", "1"]},
                    "preconditions": [],
                    "expected_effect": "command exits 1",
                    "rationale": "probe",
                },
                {
                    "id": "t1",
                    "kind": "EXECUTE",
                    "subgoal_id": "s1",
                    "tool": "filesystem.write",
                    "arguments": {"path": "beta.txt", "content": "never"},
                    "preconditions": [],
                    "expected_effect": "beta.txt exists",
                    "rationale": "write beta",
                    "depends_on": ["t0"],
                },
            ],
        }
    )
    monitor = json.dumps(
        {
            "verdicts": [
                {"candidate_id": "c1", "accepted": True, "reason": "ok"},
                {"candidate_id": "c2", "accepted": True, "reason": "ok"},
            ]
        }
    )

    app = make_app(
        tmp_path,
        brain_planner=True,
        scripts=[("goal_objective", decomposition_json), ("CANDIDATES AND CONTEXT", monitor)],
    )
    summary = await app.run_goal(
        GOAL,
        predicates=[{"type": "file_exists", "path": "beta.txt"}],
        task_id="brain4a_blocked",
    )

    # Task 1 never executes because task 0 failed
    assert not (tmp_path / "ws" / "beta.txt").exists()
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)


async def test_legacy_path_unchanged_with_brain_planner_flag(tmp_path: Path) -> None:
    """Compatibility: brain_planner=False leaves legacy path untouched."""
    app = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(
        "Create file report.txt",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="legacy4a",
    )
    assert summary.status is TaskStatus.SUCCESS


async def test_resume_does_not_repeat_side_effects(tmp_path: Path) -> None:
    """After a successful mission, resume does not re-execute tasks.

    Release-blocking invariant (SPEC §56.14): the completed side effect is
    durably recorded; reconstructing the app from the persisted checkpoint
    and resuming must reconcile task state, execution intent, and ledger
    result WITHOUT repeating the side effect. The assertion is the REAL
    side-effect count (durable ledger + filesystem), not a status alone."""
    app1 = make_app(
        tmp_path,
        brain_planner=True,
        scripts=brain_scripts(candidate_json()),
    )
    summary = await app1.run_goal(
        "Create file report.txt",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="brain4a_resume",
    )
    assert summary.status is TaskStatus.SUCCESS
    assert summary.executions == 1  # exactly one side effect
    report = next(iter((tmp_path / "ws").rglob("report.txt")))
    assert report.read_text(encoding="utf-8") == "HELLO-GRAPH"

    # Durable evidence BEFORE resume: one real execution + one completed intent
    config = {"configurable": {"thread_id": "nomadic:brain4a_resume"}}
    values1 = dict(app1.graph.get_state(config).values or {})
    execs_before = list(values1.get("executions") or [])
    assert len(execs_before) == 1
    fp = execs_before[0].action_fingerprint
    assert app1.executor.recorded_result(fp) is not None
    await app1.aclose()

    # Resume: a NEW app over the SAME durable state; the completed side
    # effect is reconciled from the ledger, never repeated.
    app2 = make_app(tmp_path, brain_planner=True, scripts=brain_scripts(candidate_json()))
    restored = await app2.resume_task("brain4a_resume")
    assert restored.status is TaskStatus.SUCCESS
    assert restored.executions == 0  # no NEW executions on resume
    # the side effect happened exactly once in the real filesystem
    assert report.read_text(encoding="utf-8") == "HELLO-GRAPH"
    # the completed task stays completed in the durable checkpoint
    config2 = {"configurable": {"thread_id": "nomadic:brain4a_resume"}}
    values2 = dict(app2.graph.get_state(config2).values or {})
    tmap = values2.get("task_status_map") or {}
    assert tmap and all(v == "COMPLETED" for v in tmap.values())
    assert len(list(values2.get("executions") or [])) == 1
    assert len(app2.mock.calls) == 0  # resume performed zero model/worker work
    await app2.aclose()
