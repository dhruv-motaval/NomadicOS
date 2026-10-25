"""Orchestrator: deterministic transitions, progression, completion,
invalid-transition rejection, replay (Sprint 1; NomadicBrain spec §21)."""

from __future__ import annotations

import pytest

from nomadicos.brain.errors import InvalidBrainState, InvalidTaskGraph, InvalidTransition
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.planner.orchestrator import Orchestrator
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
from nomadicos.brain.schemas import Subgoal
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal


def _two_root_graph(goal: Goal) -> TaskGraph:
    return TaskGraph.build(
        goal_id=goal.id,
        nodes=[
            TaskNode(id="a", description="A"),
            TaskNode(id="b", description="B"),
            TaskNode(id="c", description="C", depends_on=["a", "b"]),
        ],
    )


def test_initial_state_runnable_roots(state: BrainState) -> None:
    orchestrator = Orchestrator()
    assert orchestrator.runnable(state) == ["a"]
    assert orchestrator.is_graph_complete(state) is False
    assert orchestrator.active_subgoal(state) is None


def test_chain_dependency_progression_to_completion(state: BrainState) -> None:
    orchestrator = Orchestrator()
    assert orchestrator.runnable(state) == ["a"]
    state = orchestrator.start_task(state, "a")
    assert orchestrator.runnable(state) == []  # b waits on ACTIVE a
    state = orchestrator.complete_task(state, "a")
    assert orchestrator.runnable(state) == ["b"]
    state = orchestrator.start_task(state, "b")
    state = orchestrator.complete_task(state, "b")
    assert orchestrator.runnable(state) == ["c"]
    state = orchestrator.start_task(state, "c")
    state = orchestrator.complete_task(state, "c")
    assert orchestrator.runnable(state) == []
    assert orchestrator.is_graph_complete(state) is True
    assert state.task_graph.status("a") is TaskNodeStatus.COMPLETED
    assert state.task_graph.status("b") is TaskNodeStatus.COMPLETED
    assert state.task_graph.status("c") is TaskNodeStatus.COMPLETED


def test_branching_unlock_is_deterministic(goal: Goal) -> None:
    nodes = [
        TaskNode(id="a", description="A"),
        TaskNode(id="b", description="B", depends_on=["a"]),
        TaskNode(id="c", description="C", depends_on=["a"]),
        TaskNode(id="d", description="D", depends_on=["b", "c"]),
    ]
    graph = TaskGraph.build(goal_id=goal.id, nodes=nodes)
    state = BrainState.for_goal(goal, task_graph=graph, mission_id="mission_branch")
    orchestrator = Orchestrator()
    state = orchestrator.complete_task(orchestrator.start_task(state, "a"), "a")
    assert orchestrator.runnable(state) == ["b", "c"]  # deterministic order
    state = orchestrator.complete_task(orchestrator.start_task(state, "b"), "b")
    assert orchestrator.runnable(state) == ["c"]  # d still waits on c
    state = orchestrator.complete_task(orchestrator.start_task(state, "c"), "c")
    assert orchestrator.runnable(state) == ["d"]


def test_active_subgoal_lifecycle(goal: Goal) -> None:
    subgoal = Subgoal(id="subgoal_sg", goal_id=goal.id, description="Prepare environment")
    nodes = [
        TaskNode(id="a", description="A"),
        TaskNode(id="b", description="B", depends_on=["a"], subgoal_id="subgoal_sg"),
    ]
    graph = TaskGraph.build(goal_id=goal.id, nodes=nodes, subgoals=[subgoal])
    state = BrainState.for_goal(goal, task_graph=graph, mission_id="mission_sg")
    orchestrator = Orchestrator()
    assert orchestrator.active_subgoal(state) is None
    state = orchestrator.complete_task(orchestrator.start_task(state, "a"), "a")
    state = orchestrator.start_task(state, "b")
    assert orchestrator.active_subgoal(state) == subgoal
    state = orchestrator.complete_task(state, "b")
    assert orchestrator.active_subgoal(state) is None


def test_invalid_transitions_rejected(state: BrainState, goal: Goal) -> None:
    orchestrator = Orchestrator()
    # starting a PENDING task (dependency not satisfied)
    with pytest.raises(InvalidTransition, match="not runnable"):
        orchestrator.start_task(state, "b")
    # unknown task
    with pytest.raises(InvalidTaskGraph):
        orchestrator.start_task(state, "ghost")
    with pytest.raises(InvalidTaskGraph):
        orchestrator.complete_task(state, "ghost")
    with pytest.raises(InvalidTaskGraph):
        orchestrator.fail_task(state, "ghost")
    # completing/failing a task that was never started
    with pytest.raises(InvalidTransition, match="not ACTIVE"):
        orchestrator.complete_task(state, "a")
    with pytest.raises(InvalidTransition, match="not ACTIVE"):
        orchestrator.fail_task(state, "a")
    # completing twice
    started = orchestrator.start_task(state, "a")
    done = orchestrator.complete_task(started, "a")
    with pytest.raises(InvalidTransition, match="not ACTIVE"):
        orchestrator.complete_task(done, "a")


def test_second_active_task_rejected(goal: Goal) -> None:
    """Phase 1 is single-active-task: starting b while a is ACTIVE fails
    even though b is itself READY."""
    state = BrainState.for_goal(goal, task_graph=_two_root_graph(goal), mission_id="mission_two")
    orchestrator = Orchestrator()
    started = orchestrator.start_task(state, "a")
    with pytest.raises(InvalidTransition, match="already active"):
        orchestrator.start_task(started, "b")


def test_fail_propagates_blocked_transitively(goal: Goal) -> None:
    nodes = [
        TaskNode(id="a", description="A"),
        TaskNode(id="b", description="B", depends_on=["a"]),
        TaskNode(id="c", description="C", depends_on=["b"]),
    ]
    graph = TaskGraph.build(goal_id=goal.id, nodes=nodes)
    state = BrainState.for_goal(goal, task_graph=graph, mission_id="mission_fail")
    orchestrator = Orchestrator()
    state = orchestrator.start_task(state, "a")
    state = orchestrator.fail_task(state, "a")
    assert state.task_graph.status("a") is TaskNodeStatus.FAILED
    assert state.task_graph.status("b") is TaskNodeStatus.BLOCKED
    assert state.task_graph.status("c") is TaskNodeStatus.BLOCKED  # transitive
    assert orchestrator.runnable(state) == []
    assert orchestrator.is_graph_complete(state) is False
    # blocked dependents cannot be started
    with pytest.raises(InvalidTransition, match="not runnable"):
        orchestrator.start_task(state, "b")


def test_failure_blocks_only_dependents(goal: Goal) -> None:
    nodes = [
        TaskNode(id="a", description="A"),
        TaskNode(id="b", description="B", depends_on=["a"]),
        TaskNode(id="c", description="C", depends_on=["a"]),
        TaskNode(id="d", description="D", depends_on=["b"]),
    ]
    graph = TaskGraph.build(goal_id=goal.id, nodes=nodes)
    state = BrainState.for_goal(goal, task_graph=graph, mission_id="mission_partial")
    orchestrator = Orchestrator()
    state = orchestrator.complete_task(orchestrator.start_task(state, "a"), "a")
    state = orchestrator.fail_task(orchestrator.start_task(state, "b"), "b")
    assert state.task_graph.status("d") is TaskNodeStatus.BLOCKED
    assert orchestrator.runnable(state) == ["c"]  # the independent branch continues
    assert orchestrator.is_graph_complete(state) is False


def test_transitions_are_pure_and_versioned(state: BrainState) -> None:
    orchestrator = Orchestrator()
    original_json = state.to_canonical_json()
    started = orchestrator.start_task(state, "a")
    assert state.task_graph.status("a") is TaskNodeStatus.READY  # input untouched
    assert state.to_canonical_json() == original_json
    assert started.state_version == state.state_version + 1
    completed = orchestrator.complete_task(started, "a")
    assert completed.state_version == state.state_version + 2
    assert started.task_graph.status("a") is TaskNodeStatus.ACTIVE  # intermediate untouched


def test_deterministic_replay_of_the_same_input(state: BrainState) -> None:
    """Core Sprint 1 invariant: the same state and transition sequence
    produce identical trajectories — no model, no randomness, no clock."""

    def run(initial: BrainState) -> BrainState:
        orchestrator = Orchestrator()
        current = initial
        for task_id in ("a", "b", "c"):
            current = orchestrator.start_task(current, task_id)
            current = orchestrator.complete_task(current, task_id)
            current = current.with_event(
                BrainEvent(
                    event_id=f"evt_done_{task_id}",
                    mission_id=current.mission_id,
                    event_type=BrainEventType.BRAIN_TASK_COMPLETED,
                    payload={"task": task_id},
                )
            )
        return current

    first = run(state)
    second = run(state)
    assert first == second
    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.state_version == state.state_version + 9  # start+complete+event per task
    assert len(first.history) == 3
    assert Orchestrator().is_graph_complete(first) is True


def test_completion_is_representation_not_goal_success(state: BrainState) -> None:
    """Structural completion never claims goal SUCCESS: the goal object is
    untouched, and BrainState has no model- or brain-settable success
    field (NomadicOS SPEC §27-29, §56.8-10)."""
    orchestrator = Orchestrator()
    goal_before = state.goal
    current = state
    for task_id in ("a", "b", "c"):
        current = orchestrator.complete_task(orchestrator.start_task(current, task_id), task_id)
    assert orchestrator.is_graph_complete(current) is True
    assert current.goal == goal_before  # the success definition is immutable
    assert "success" not in BrainState.model_fields
    assert "status" not in BrainState.model_fields


def test_empty_graph_stops_without_completion(goal: Goal) -> None:
    state = BrainState.for_goal(goal, mission_id="mission_empty")
    orchestrator = Orchestrator()
    assert orchestrator.runnable(state) == []
    assert orchestrator.is_graph_complete(state) is False  # unplanned != done


def test_transitions_reject_tampered_graph_goal_binding(goal: Goal, state: BrainState) -> None:
    """A transition can never smuggle a graph for a different goal: the
    controller re-checks goal binding on every transition (fail closed)."""
    orchestrator = Orchestrator()
    tampered = state.model_copy(
        update={"task_graph": TaskGraph.build(goal_id="task_other", nodes=[])}
    )
    with pytest.raises(InvalidBrainState, match="not bound"):
        orchestrator.start_task(tampered, "a")
