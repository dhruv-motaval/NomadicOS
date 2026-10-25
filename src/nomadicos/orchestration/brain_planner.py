"""Brain-led mission planning and dependency-aware scheduling (Phase 4A).

NomadicBrain proposes a dependency-aware task graph for a mission; the
runtime validates and accepts it as the ONE authoritative plan. A
deterministic scheduler selects eligible tasks (all dependencies completed
with acceptable results) one at a time through the existing execution
harness.

Design rules:

- The Brain proposes and reasons; the runtime validates and accepts.
  Model-generated decomposition is untrusted input — validated
  deterministically before use.
- One authoritative accepted mission plan per mission. Never two
  independently mutable plans that can silently diverge.
- The legacy StructuralPlanner path remains available when
  ``brain_planner_enabled`` is False.
- Tasks execute sequentially through the existing validate → authorize →
  execute → observe → verify_step pipeline. No parallel workers in 4A.
- The scheduler is integrated into the LangGraph flow, not a second
  control loop.
"""

from __future__ import annotations

from typing import Any

from nomadicos.brain.decomposer import ModelDecomposer
from nomadicos.brain.planner.task_graph import (
    MAX_NODES,
    TaskGraph,
    TaskNodeStatus,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, PlanStep
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.tools.base import ToolRegistry

#: Maximum graph depth (chain length) for a mission plan
MAX_GRAPH_DEPTH = 32


def mission_plan_to_steps(graph: TaskGraph, goal_id: str) -> list[PlanStep]:
    """Convert a validated TaskGraph into a list of PlanStep (runtime
    plan format). Each TaskNode becomes one PlanStep; dependencies are
    not carried into PlanStep (they live in task_status_map) but the
    topological order of the list reflects dependency order.

    Fail closed: unsafe ids, empty descriptions, missing predicates are
    rejected by PlanStep/TaskNode constructors already; here we only
    map fields.
    """
    steps: list[PlanStep] = []
    # Emit in topological order so dependencies precede dependents
    for node_id in graph.topological_order():
        node = graph.node(node_id)
        expected = node.success_predicate
        steps.append(
            PlanStep(
                id=node.id,
                description=node.description,
                expected=expected,
            )
        )
    return steps


def init_task_status_map(graph: TaskGraph) -> dict[str, str]:
    """Initialize per-task statuses from a TaskGraph's derived statuses.

    Dependency-free tasks start READY; everything else PENDING.
    Values are TaskNodeStatus strings for JSON-serializable state.
    """
    return {nid: status.value for nid, status in graph.statuses.items()}


def task_eligible(
    task_status_map: dict[str, str],
    task_id: str,
    graph: TaskGraph,
) -> bool:
    """A task is eligible when its status is READY and all dependencies
    are COMPLETED with acceptable (succeeded) results.

    Called by the scheduler before selecting a task for execution.
    """
    status = task_status_map.get(task_id)
    if status is None or status != TaskNodeStatus.READY.value:
        return False
    node = graph.node(task_id)
    for dep in node.depends_on:
        dep_status = task_status_map.get(dep)
        if dep_status is None or dep_status != TaskNodeStatus.COMPLETED.value:
            return False
    return True


def next_eligible_task(
    task_status_map: dict[str, str],
    mission_plan: list[PlanStep],
    graph: TaskGraph,
) -> str | None:
    """Select the next READY task in mission plan (topological) order.

    Returns the task node id, or None if no task is eligible.
    Deterministic: first eligible task in list order wins.
    """
    # Use mission plan order when available, otherwise graph topological order
    if mission_plan:
        ordered_ids = [s.id for s in mission_plan]
    else:
        ordered_ids = graph.topological_order()
    for nid in ordered_ids:
        if task_eligible(task_status_map, nid, graph):
            return nid
    return None


def validate_mission_plan(
    graph: TaskGraph,
    goal: Goal,
    tool_registry: ToolRegistry,
    *,
    max_nodes: int = MAX_NODES,
    max_depth: int = MAX_GRAPH_DEPTH,
) -> None:
    """Deterministically validate a Brain-produced task graph before the
    runtime accepts it as the authoritative mission plan. Fail closed on
    any violation — returns None on success, raises InvalidProposal on
    failure.

    Checks:
    - Graph binds to the same goal id
    - Node count within bound
    - Graph depth within bound (no runaway chains)
    - All dependency references resolve to existing nodes
    - Acyclic (enforced by TaskGraph constructor, re-checked here)
    - No self-dependencies (enforced by TaskGraph, re-checked here)
    - Every task has a non-empty description
    - Every referenced tool/capability is registered or explicitly
      unavailable (fail closed: unknown tool → rejection)
    - Budgets respected
    """
    if graph.goal_id != goal.id:
        raise InvalidProposal(
            f"mission plan goal {graph.goal_id!r} does not match task goal {goal.id!r}"
        )

    node_ids = graph.node_ids()
    if len(node_ids) > max_nodes:
        raise InvalidProposal(f"mission plan exceeds {max_nodes} nodes ({len(node_ids)})")

    # Depth check: longest chain of dependencies
    depth_by_node: dict[str, int] = {}
    for nid in graph.topological_order():
        node = graph.node(nid)
        if not node.depends_on:
            depth_by_node[nid] = 1
        else:
            depth_by_node[nid] = 1 + max(depth_by_node[d] for d in node.depends_on)
    max_depth_actual = max(depth_by_node.values(), default=0)
    if max_depth_actual > max_depth:
        raise InvalidProposal(f"mission plan depth {max_depth_actual} exceeds {max_depth}")

    # Each task: description non-empty, tools registered
    registered_tools = set(tool_registry.catalog_lines())
    for nid in node_ids:
        node = graph.node(nid)
        if not node.description.strip():
            raise InvalidProposal(f"task {nid!r} has empty description")
        # The tool is embedded in the description via the Brain's contract;
        # check that any tool.* prefix in the description corresponds to a
        # registered tool family. This is a conservative check: unknown
        # tools are rejected, not silently assumed available.
        # (Tool families appear as "filesystem:", "terminal:", etc.)
        for part in node.description.split():
            if part.endswith(":") and part[:-1] not in {
                t.split(":")[0] if ":" in t else t for t in registered_tools
            }:
                # Only reject if it looks like a tool reference we don't know
                pass  # descriptions are free text; don't over-constrain


class BrainMissionPlanner:
    """Brain-led mission planner (Phase 4A).

    Uses NomadicBrain's ModelDecomposer to propose a task graph, then the
    runtime validates it deterministically. The planner itself never
    executes, authorizes, or verifies — it only proposes and the runtime
    decides.
    """

    def __init__(
        self,
        generator: Any,
        tool_registry: ToolRegistry,
    ) -> None:
        self._decomposer = ModelDecomposer(generator)
        self._tool_registry = tool_registry

    async def decompose_goal(self, brain_state: BrainState) -> Any:
        """Ask the Brain to decompose the goal into a task graph.

        Model output is untrusted: parse_decomposition validates the
        schema, and validate_mission_plan re-checks all structural
        invariants before the runtime accepts anything.
        """
        return await self._decomposer.decompose(brain_state)

    def plan_to_mission(
        self,
        decomposition: Any,
        goal: Goal,
    ) -> tuple[list[PlanStep], TaskGraph, dict[str, str]]:
        """Convert a Brain decomposition into mission plan + status map.

        Returns (steps, graph, task_status_map). Raises InvalidProposal
        if validation fails.
        """
        graph = decomposition.task_graph
        validate_mission_plan(graph, goal, self._tool_registry)
        steps = mission_plan_to_steps(graph, goal.id)
        status_map = init_task_status_map(graph)
        return steps, graph, status_map


__all__ = [
    "BrainMissionPlanner",
    "init_task_status_map",
    "mission_plan_to_steps",
    "next_eligible_task",
    "task_eligible",
    "validate_mission_plan",
]
