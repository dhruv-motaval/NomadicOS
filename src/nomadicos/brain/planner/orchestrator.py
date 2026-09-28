"""Deterministic orchestrator — foundation level (NomadicBrain spec §21,
Sprint 1).

A pure state-transition controller over ``BrainState``/``TaskGraph``.
It computes runnable nodes, advances task status deterministically,
maintains the active subgoal, stops when graph completion is
represented, and rejects every invalid transition (fail closed).

It does NOT: call models, decompose goals, execute tools, authorize,
verify, search, or recover — those belong to later brain sprints and to
the existing NomadicOS boundaries (Action IR, authority, executor,
verification), which the brain never bypasses.

Determinism: transitions are pure functions of the input state — no
ids, timestamps, randomness, or I/O are minted inside; replaying the
same transition sequence from the same state yields identical states.

Single-active-task policy: Phase 1 advances ONE task at a time;
starting a task while another is ACTIVE is rejected.

Completion semantics: ``COMPLETED`` nodes and ``is_graph_complete`` are
REPRESENTED states — step/task completion is never goal SUCCESS
(NomadicOS SPEC §27-29, §56.8-10). The brain performs no goal
verification in Phase 1.
"""

from __future__ import annotations

from nomadicos.brain.errors import InvalidBrainState, InvalidTransition
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
from nomadicos.brain.schemas import Subgoal
from nomadicos.brain.state import BrainState

_BLOCKING = (TaskNodeStatus.FAILED, TaskNodeStatus.BLOCKED)


def _require_bound(state: BrainState) -> None:
    """Fail closed when the state's graph is not bound to the state's goal
    (defense against validator-bypassing model_copy tampering)."""
    if state.task_graph.goal_id != state.goal.id:
        raise InvalidBrainState(
            "task graph is not bound to the state's goal",
            graph_goal=state.task_graph.goal_id,
            goal_id=state.goal.id,
        )


def _rebuild(graph: TaskGraph, statuses: dict[str, TaskNodeStatus]) -> TaskGraph:
    """Re-construct the graph with new statuses, re-running ALL graph
    validators: a transition can never smuggle an incoherent graph
    through ``model_copy``'s validator bypass (fail closed)."""
    return TaskGraph(
        goal_id=graph.goal_id,
        subgoals=graph.subgoals,
        nodes=graph.nodes,
        statuses=statuses,
    )


def _require_active(graph: TaskGraph, node_id: str) -> TaskNode:
    """Fail closed unless ``node_id`` exists and is ACTIVE."""
    node = graph.node(node_id)  # unknown ids raise InvalidTaskGraph
    status = graph.status(node_id)
    if status is not TaskNodeStatus.ACTIVE:
        raise InvalidTransition(
            f"task {node_id!r} is {status.value}, not ACTIVE",
            node_id=node_id,
            graph_goal=graph.goal_id,
        )
    return node


class Orchestrator:
    """Deterministic runtime control (foundation). Stateless: every method
    is a pure transition/query over the caller's state."""

    # ----------------------------------------------------------- queries ---

    def runnable(self, state: BrainState) -> list[str]:
        """Task ids that may start now (READY), in deterministic order."""
        return state.task_graph.ready_ids()

    def is_graph_complete(self, state: BrainState) -> bool:
        """Whether graph completion is represented (all tasks COMPLETED).
        Structural representation only — never goal SUCCESS."""
        return state.task_graph.complete

    def active_subgoal(self, state: BrainState) -> Subgoal | None:
        """The subgoal of the mission's current focus, if any."""
        return state.active_subgoal

    # -------------------------------------------------------- transitions ---

    def start_task(self, state: BrainState, node_id: str) -> BrainState:
        """READY -> ACTIVE. Rejects unknown tasks, non-runnable tasks, and
        a second concurrent active task. Sets the active subgoal from
        the started task's subgoal reference."""
        graph = state.task_graph
        _require_bound(state)
        node = graph.node(node_id)
        status = graph.status(node_id)
        if status is not TaskNodeStatus.READY:
            raise InvalidTransition(
                f"task {node_id!r} is {status.value}, not runnable",
                node_id=node_id,
                graph_goal=graph.goal_id,
            )
        if graph.ids_with_status(TaskNodeStatus.ACTIVE):
            raise InvalidTransition(
                "a task is already active; complete or fail it first",
                node_id=node_id,
                graph_goal=graph.goal_id,
            )
        statuses = dict(graph.statuses)
        statuses[node_id] = TaskNodeStatus.ACTIVE
        new_graph = _rebuild(graph, statuses)
        subgoal = (
            next((s for s in new_graph.subgoals if s.id == node.subgoal_id), None)
            if node.subgoal_id is not None
            else None
        )
        return state.model_copy(
            update={
                "task_graph": new_graph,
                "active_subgoal": subgoal,
                "state_version": state.state_version + 1,
            }
        )

    def complete_task(self, state: BrainState, node_id: str) -> BrainState:
        """ACTIVE -> COMPLETED, then deterministically unlock every PENDING
        task whose dependencies are now all completed (PENDING -> READY).
        Clears the active subgoal."""
        graph = state.task_graph
        _require_bound(state)
        _require_active(graph, node_id)
        statuses = dict(graph.statuses)
        statuses[node_id] = TaskNodeStatus.COMPLETED
        for node in graph.nodes:
            if statuses[node.id] is TaskNodeStatus.PENDING and all(
                statuses[d] is TaskNodeStatus.COMPLETED for d in node.depends_on
            ):
                statuses[node.id] = TaskNodeStatus.READY
        return state.model_copy(
            update={
                "task_graph": _rebuild(graph, statuses),
                "active_subgoal": None,
                "state_version": state.state_version + 1,
            }
        )

    def fail_task(self, state: BrainState, node_id: str) -> BrainState:
        """ACTIVE -> FAILED, then deterministically propagate BLOCKED to
        every PENDING task that (transitively) depends on a failure.
        Clears the active subgoal. Recovery is a later sprint; failure is
        terminal in Phase 1."""
        graph = state.task_graph
        _require_bound(state)
        _require_active(graph, node_id)
        statuses = dict(graph.statuses)
        statuses[node_id] = TaskNodeStatus.FAILED
        changed = True
        while changed:
            changed = False
            for node in graph.nodes:
                if statuses[node.id] is TaskNodeStatus.PENDING and any(
                    statuses[d] in _BLOCKING for d in node.depends_on
                ):
                    statuses[node.id] = TaskNodeStatus.BLOCKED
                    changed = True
        return state.model_copy(
            update={
                "task_graph": _rebuild(graph, statuses),
                "active_subgoal": None,
                "state_version": state.state_version + 1,
            }
        )
