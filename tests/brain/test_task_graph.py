"""TaskGraph: validation, cycles, ordering, statuses, completion
(Sprint 1; NomadicBrain spec §13)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.errors import InvalidTaskGraph
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
from nomadicos.brain.schemas import Subgoal
from nomadicos.contracts.core import Goal, parse_predicate


def _nodes(*specs: tuple[str, list[str]]) -> list[TaskNode]:
    return [TaskNode(id=nid, description=f"Task {nid}", depends_on=deps) for nid, deps in specs]


def test_build_derives_initial_statuses(goal: Goal) -> None:
    graph = TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", []), ("b", ["a"]), ("c", ["b"])))
    assert graph.status("a") is TaskNodeStatus.READY
    assert graph.status("b") is TaskNodeStatus.PENDING
    assert graph.status("c") is TaskNodeStatus.PENDING
    assert graph.ready_ids() == ["a"]


def test_empty_graph_represents_not_yet_decomposed(goal: Goal) -> None:
    graph = TaskGraph.build(goal_id=goal.id, nodes=[])
    assert graph.nodes == []
    assert graph.complete is False  # no false completion


def test_duplicate_node_ids_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="duplicate task node id"):
        TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", []), ("a", [])))


def test_unknown_dependency_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="unknown task"):
        TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", ["ghost"])))


def test_dependency_cycle_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", ["b"]), ("b", ["a"])))


def test_self_dependency_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="depends on itself"):
        TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", ["a"])))


def test_duplicate_dependency_entries_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="duplicate dependencies"):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=_nodes(("a", []), ("b", ["a", "a"])),
        )


def test_dangling_subgoal_reference_rejected(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="unknown subgoal"):
        TaskGraph(
            goal_id=goal.id,
            nodes=[TaskNode(id="a", description="A", subgoal_id="subgoal_missing")],
            statuses={"a": TaskNodeStatus.READY},
        )


def test_subgoal_must_belong_to_the_graph_goal(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="does not belong to goal"):
        TaskGraph.build(
            goal_id=goal.id,
            nodes=_nodes(("a", [])),
            subgoals=[Subgoal(id="subgoal_sg", goal_id="task_other", description="x")],
        )


def test_duplicate_subgoal_ids_rejected(goal: Goal) -> None:
    subgoals = [
        Subgoal(id="subgoal_sg", goal_id=goal.id, description="one"),
        Subgoal(id="subgoal_sg", goal_id=goal.id, description="two"),
    ]
    with pytest.raises(ValidationError, match="duplicate subgoal"):
        TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", [])), subgoals=subgoals)


def test_statuses_must_cover_exactly_the_node_ids(goal: Goal) -> None:
    nodes = _nodes(("a", []), ("b", ["a"]))
    with pytest.raises(ValidationError, match="missing=\\['b'\\]"):
        TaskGraph(goal_id=goal.id, nodes=nodes, statuses={"a": TaskNodeStatus.READY})
    with pytest.raises(ValidationError, match="extra=\\['ghost'\\]"):
        TaskGraph(
            goal_id=goal.id,
            nodes=_nodes(("a", [])),
            statuses={"a": TaskNodeStatus.READY, "ghost": TaskNodeStatus.PENDING},
        )


def test_incoherent_statuses_rejected(goal: Goal) -> None:
    # PENDING root: no unfinished dependency -> must be READY
    with pytest.raises(ValidationError, match="PENDING"):
        TaskGraph(goal_id=goal.id, nodes=_nodes(("a", [])), statuses={"a": TaskNodeStatus.PENDING})
    # BLOCKED without a failed dependency
    with pytest.raises(ValidationError, match="BLOCKED"):
        TaskGraph(goal_id=goal.id, nodes=_nodes(("a", [])), statuses={"a": TaskNodeStatus.BLOCKED})
    # READY with an uncompleted dependency
    with pytest.raises(ValidationError, match="uncompleted dependencies"):
        TaskGraph(
            goal_id=goal.id,
            nodes=_nodes(("a", []), ("b", ["a"])),
            statuses={"a": TaskNodeStatus.READY, "b": TaskNodeStatus.READY},
        )
    # COMPLETED with an uncompleted dependency
    with pytest.raises(ValidationError, match="uncompleted dependencies"):
        TaskGraph(
            goal_id=goal.id,
            nodes=_nodes(("a", []), ("b", ["a"])),
            statuses={"a": TaskNodeStatus.READY, "b": TaskNodeStatus.COMPLETED},
        )


def test_coherent_all_completed_graph_is_valid_and_complete(goal: Goal) -> None:
    graph = TaskGraph(
        goal_id=goal.id,
        nodes=_nodes(("a", []), ("b", ["a"])),
        statuses={"a": TaskNodeStatus.COMPLETED, "b": TaskNodeStatus.COMPLETED},
    )
    assert graph.complete is True


def test_topological_order_is_deterministic_and_respects_dependencies(goal: Goal) -> None:
    nodes = _nodes(("c", ["b"]), ("a", []), ("b", ["a"]))
    graph = TaskGraph.build(goal_id=goal.id, nodes=nodes)
    order = graph.topological_order()
    assert sorted(order) == ["a", "b", "c"]
    assert order.index("a") < order.index("b") < order.index("c")
    # stable: repeated calls and fresh builds produce the same order
    assert graph.topological_order() == order
    rebuilt = TaskGraph.build(goal_id=goal.id, nodes=nodes)
    assert rebuilt.topological_order() == order


def test_topological_order_stable_first_eligible_tie_break(goal: Goal) -> None:
    # repeated-first-eligible: y unblocks x before z is considered
    graph = TaskGraph.build(goal_id=goal.id, nodes=_nodes(("x", ["y"]), ("y", []), ("z", [])))
    assert graph.topological_order() == ["y", "x", "z"]


def test_lookups_fail_closed(goal: Goal) -> None:
    graph = TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", [])))
    assert graph.node("a").id == "a"
    assert graph.status("a") is TaskNodeStatus.READY
    with pytest.raises(InvalidTaskGraph):
        graph.node("ghost")
    with pytest.raises(InvalidTaskGraph):
        graph.status("ghost")


def test_serialization_round_trip_is_deterministic(goal: Goal) -> None:
    graph = TaskGraph.build(goal_id=goal.id, nodes=_nodes(("a", []), ("b", ["a"])))
    restored = TaskGraph.model_validate_json(graph.model_dump_json())
    assert restored == graph
    assert restored.topological_order() == graph.topological_order()


def test_node_description_and_id_validation(goal: Goal) -> None:
    with pytest.raises(ValidationError, match="unsafe task node id"):
        TaskNode(id="bad id!", description="x")
    with pytest.raises(ValidationError, match="description"):
        TaskNode(id="a", description="   ")


def test_node_success_predicate_is_explicit(goal: Goal) -> None:
    node = TaskNode(
        id="a",
        description="A",
        success_predicate=parse_predicate({"type": "file_exists", "path": "out.txt"}),
    )
    assert node.success_predicate is not None
    assert node.success_predicate.predicate is not None
    assert node.success_predicate.predicate.field("path") == "out.txt"
    bare = TaskNode(id="b", description="B")
    assert bare.success_predicate is None  # no predicate -> no silent success
