"""Deterministic task graph (NomadicBrain spec §13, Sprint 1).

Structure only: the graph represents tasks, subgoals, dependencies, and
statuses. It never executes tools, never calls models, and never decides
authorization or verification. Invalid graphs fail closed at
construction: duplicate ids, unknown or cyclic dependencies, dangling
subgoal references, and incoherent status maps are rejected.

Status semantics:

- PENDING    has unfinished (non-completed) dependencies
- READY      all dependencies completed; runnable now
- ACTIVE     started; in progress
- COMPLETED  done — a represented state, NEVER goal SUCCESS (NomadicOS
             SPEC §27-29: SUCCESS requires independent verification)
- FAILED     failed; terminal in Phase 1 (recovery is a later sprint)
- BLOCKED    a transitive dependency failed

Task completion is graph bookkeeping, not goal completion.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.errors import InvalidTaskGraph
from nomadicos.brain.schemas import MAX_DESCRIPTION, MAX_NODE_ID, Subgoal
from nomadicos.contracts.core import Contract, GoalPredicate

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

#: hard ceiling on graph width (bounded structure, NomadicOS SPEC §14)
MAX_NODES = 128
MAX_DEPENDENCIES = 16


def is_safe_node_id(node_id: str) -> bool:
    """Whether ``node_id`` is a bounded, injection-safe identifier.

    Shared by the Sprint 2 cognitive modules (decomposer, actor): any
    model-authored id must pass this before it can enter a contract.
    """
    return bool(_NODE_ID_RE.fullmatch(node_id))


class TaskNodeStatus(StrEnum):
    """Deterministic task lifecycle states (structure, not outcome truth)."""

    PENDING = "PENDING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskNode(Contract):
    """One task in the graph: explicit id, explicit dependencies, optional
    subgoal reference and explicit success predicate (spec §13).

    A node without a success predicate never silently counts as success:
    later verifiers treat it as not verifiable (fail closed).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    subgoal_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    success_predicate: GoalPredicate | None = None

    @model_validator(mode="after")
    def _coherent(self) -> TaskNode:
        if not _NODE_ID_RE.fullmatch(self.id):
            raise ValueError(f"unsafe task node id {self.id!r}")
        if len(self.id) > MAX_NODE_ID:
            raise ValueError(f"task node id exceeds {MAX_NODE_ID} chars")
        if not self.description.strip():
            raise ValueError("task node description must not be empty")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(f"task node description exceeds {MAX_DESCRIPTION} chars")
        if len(self.depends_on) > MAX_DEPENDENCIES:
            raise ValueError(f"task node exceeds {MAX_DEPENDENCIES} dependencies")
        return self


def _emit_topological(nodes: list[TaskNode]) -> list[str]:
    """Stable topological order: repeatedly emit the FIRST node (in
    ``nodes`` list order) whose dependencies are all emitted. Raises
    ``ValueError`` when no progress is possible (dependency cycle)."""
    by_id = {n.id: n for n in nodes}
    emitted: list[str] = []
    emitted_set: set[str] = set()
    remaining = [n.id for n in nodes]
    while remaining:
        pick = next(
            (nid for nid in remaining if all(d in emitted_set for d in by_id[nid].depends_on)),
            None,
        )
        if pick is None:
            raise ValueError(f"dependency cycle among tasks {sorted(remaining)!r}")
        emitted.append(pick)
        emitted_set.add(pick)
        remaining.remove(pick)
    return emitted


class TaskGraph(Contract):
    """A validated, deterministic task DAG for one goal (spec §13).

    Immutable data: transitions produce new instances. ``statuses`` must
    cover exactly the node ids and be coherent with the dependency
    structure, so any deserialized graph is guaranteed valid (fail
    closed).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str
    subgoals: list[Subgoal] = Field(default_factory=list)
    nodes: list[TaskNode] = Field(default_factory=list)
    statuses: dict[str, TaskNodeStatus] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _valid(self) -> TaskGraph:
        if not self.goal_id.strip():
            raise ValueError("task graph goal_id must not be empty")
        if len(self.nodes) > MAX_NODES:
            raise ValueError(f"task graph exceeds {MAX_NODES} nodes")

        node_ids = [n.id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate task node id")
        id_set = set(node_ids)

        subgoal_ids = {s.id for s in self.subgoals}
        if len(subgoal_ids) != len(self.subgoals):
            raise ValueError("duplicate subgoal id")
        for subgoal in self.subgoals:
            if subgoal.goal_id != self.goal_id:
                raise ValueError(f"subgoal {subgoal.id!r} does not belong to goal {self.goal_id!r}")

        for node in self.nodes:
            if len(set(node.depends_on)) != len(node.depends_on):
                raise ValueError(f"task {node.id!r} has duplicate dependencies")
            if node.id in node.depends_on:
                raise ValueError(f"task {node.id!r} depends on itself")
            for dep in node.depends_on:
                if dep not in id_set:
                    raise ValueError(f"task {node.id!r} depends on unknown task {dep!r}")
            if node.subgoal_id is not None and node.subgoal_id not in subgoal_ids:
                raise ValueError(f"task {node.id!r} references unknown subgoal {node.subgoal_id!r}")

        _emit_topological(self.nodes)  # raises ValueError on cycles

        if set(self.statuses) != id_set:
            missing = sorted(id_set - set(self.statuses))
            extra = sorted(set(self.statuses) - id_set)
            raise ValueError(
                f"task statuses must cover exactly the task ids (missing={missing}, extra={extra})"
            )
        for node in self.nodes:
            status = self.statuses[node.id]
            deps_completed = all(
                self.statuses[d] is TaskNodeStatus.COMPLETED for d in node.depends_on
            )
            dep_blocked = any(
                self.statuses[d] in (TaskNodeStatus.FAILED, TaskNodeStatus.BLOCKED)
                for d in node.depends_on
            )
            if status is TaskNodeStatus.PENDING and (deps_completed or dep_blocked):
                raise ValueError(f"task {node.id!r} is PENDING but its dependencies allow more")
            if status is TaskNodeStatus.BLOCKED and not dep_blocked:
                raise ValueError(f"task {node.id!r} is BLOCKED without a failed dependency")
            if (
                status
                in (
                    TaskNodeStatus.READY,
                    TaskNodeStatus.ACTIVE,
                    TaskNodeStatus.COMPLETED,
                    TaskNodeStatus.FAILED,
                )
                and not deps_completed
            ):
                raise ValueError(
                    f"task {node.id!r} is {status.value} with uncompleted dependencies"
                )
        return self

    @classmethod
    def build(
        cls,
        *,
        goal_id: str,
        nodes: list[TaskNode] | None = None,
        subgoals: list[Subgoal] | None = None,
    ) -> TaskGraph:
        """Construct a graph with derived initial statuses: dependency-free
        tasks are READY, everything else PENDING. No nodes means the goal
        is not yet decomposed (an empty graph is never complete)."""
        task_nodes = list(nodes or [])
        statuses = {
            n.id: (TaskNodeStatus.READY if not n.depends_on else TaskNodeStatus.PENDING)
            for n in task_nodes
        }
        return cls(
            goal_id=goal_id,
            subgoals=list(subgoals or []),
            nodes=task_nodes,
            statuses=statuses,
        )

    # ---------------------------------------------------------- queries ---

    def node_ids(self) -> list[str]:
        """Node ids in deterministic (list) order."""
        return [n.id for n in self.nodes]

    def node(self, node_id: str) -> TaskNode:
        """Look up one task node; unknown ids fail closed."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise InvalidTaskGraph(f"unknown task node id {node_id!r}", graph_goal=self.goal_id)

    def status(self, node_id: str) -> TaskNodeStatus:
        """Status of one task; unknown ids fail closed."""
        try:
            return self.statuses[node_id]
        except KeyError:
            raise InvalidTaskGraph(
                f"unknown task node id {node_id!r}", graph_goal=self.goal_id
            ) from None

    def ready_ids(self) -> list[str]:
        """Runnable task ids (READY) in deterministic graph order."""
        return self.ids_with_status(TaskNodeStatus.READY)

    def ids_with_status(self, status: TaskNodeStatus) -> list[str]:
        """Ids whose status equals ``status``, in deterministic graph order."""
        return [n.id for n in self.nodes if self.statuses[n.id] is status]

    def topological_order(self) -> list[str]:
        """Deterministic topological order (stable: first eligible in list
        order wins). The graph is acyclic by construction; a cycle here is
        a structural bug and fails closed."""
        try:
            return _emit_topological(self.nodes)
        except ValueError as exc:
            raise InvalidTaskGraph(str(exc), graph_goal=self.goal_id) from None

    @property
    def complete(self) -> bool:
        """True when graph completion is REPRESENTED: every task COMPLETED.

        This is structural bookkeeping, never goal SUCCESS — SUCCESS
        requires independent goal-predicate verification (NomadicOS SPEC
        §27-29). An empty graph is never complete (no false completion).
        """
        return bool(self.statuses) and all(
            s is TaskNodeStatus.COMPLETED for s in self.statuses.values()
        )
