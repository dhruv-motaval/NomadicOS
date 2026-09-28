"""Decomposer: typed model output, fail-closed validation, deterministic
task graph generation, goal binding (Sprint 2; NomadicBrain spec §13)."""

from __future__ import annotations

import pytest

from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.decomposer import (
    ModelDecomposer,
    apply_decomposition,
    parse_decomposition,
)
from nomadicos.brain.errors import InvalidBrainState, InvalidTransition, ModelOutputRejected
from nomadicos.brain.planner.task_graph import TaskNodeStatus
from nomadicos.brain.schemas import canonical_json
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal

from .conftest import FakeGenerator

VALID = (
    '{"subgoals": [{"id": "sg1", "description": "Prepare workspace", '
    '"success_predicate": {"type": "directory_exists", "path": "work"}}], '
    '"tasks": ['
    '{"id": "t1", "description": "Create dir", "subgoal_id": "sg1", '
    '"depends_on": [], "success_predicate": {"type": "directory_exists", "path": "out"}}, '
    '{"id": "t2", "description": "Write output", "subgoal_id": "sg1", '
    '"depends_on": ["t1"], "success_predicate": {"type": "file_exists", "path": "out.txt"}}]}'
)


def _fresh_state(goal_id: str, objective: str = "Produce out.txt") -> BrainState:
    return BrainState.for_goal(
        Goal(id=goal_id, objective=objective),
        mission_id=f"mission_{goal_id}",
    )


async def test_valid_decomposition_produces_typed_graph() -> None:
    state = _fresh_state()
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, VALID)
    decomposition = await ModelDecomposer(generator).decompose(state)  # type: ignore[arg-type]

    assert [node.id for node in decomposition.task_graph.nodes] == ["t1", "t2"]
    assert decomposition.task_graph.node("t2").depends_on == ["t1"]
    assert decomposition.task_graph.subgoals[0].goal_id == state.goal.id
    assert decomposition.task_graph.node("t1").success_predicate is not None
    assert decomposition.task_graph.node("t2").success_predicate is not None

    planned = apply_decomposition(state, decomposition)
    assert planned.task_graph.goal_id == state.goal.id
    assert planned.task_graph.status("t1") is TaskNodeStatus.READY
    assert planned.task_graph.status("t2") is TaskNodeStatus.PENDING
    assert planned.state_version == state.state_version + 1


async def test_apply_decomposition_preserves_state_fields() -> None:
    state = _fresh_state()
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, VALID)
    decomposition = await ModelDecomposer(generator).decompose(state)  # type: ignore[arg-type]

    planned = apply_decomposition(state, decomposition)
    assert planned.mission_id == state.mission_id
    assert planned.goal == state.goal
    assert planned.history == state.history
    assert planned.task_graph is not state.task_graph


async def test_task_graph_generation_is_deterministic() -> None:
    first = parse_decomposition(VALID, goal_id="task_d")
    second = parse_decomposition(VALID, goal_id="task_d")
    assert first == second
    assert canonical_json(first.task_graph) == canonical_json(second.task_graph)
    assert first.task_graph is not second.task_graph


async def test_malformed_output_fails_closed() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, "not json at all")
    decomposer = ModelDecomposer(generator)  # type: ignore[arg-type]
    with pytest.raises(ModelOutputRejected):
        await decomposer.decompose(_fresh_state())


async def test_malformed_output_unknown_fields_fail_closed() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.DECOMPOSER,
        '{"subgoals": [], "tasks": [{"id": "t1", "description": "d", "authorized": true}]}',
    )
    decomposer = ModelDecomposer(generator)  # type: ignore[arg-type]
    with pytest.raises(ModelOutputRejected, match="schema validation failed"):
        await decomposer.decompose(_fresh_state())


async def test_empty_or_missing_tasks_fail_closed() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, '{"subgoals": [], "tasks": []}', '{"subgoals": []}')
    decomposer = ModelDecomposer(generator)  # type: ignore[arg-type]
    state = _fresh_state()
    with pytest.raises(ModelOutputRejected):
        await decomposer.decompose(state)
    with pytest.raises(ModelOutputRejected):
        await decomposer.decompose(state)


async def test_invalid_dependency_fails_closed() -> None:
    text = (
        '{"subgoals": [], "tasks": ['
        '{"id": "t1", "description": "A"}, '
        '{"id": "t2", "description": "B", "depends_on": ["missing"]}]}'
    )
    with pytest.raises(ModelOutputRejected, match="unknown task"):
        parse_decomposition(text, goal_id="task_g")


async def test_cycle_rejection_fails_closed() -> None:
    text = (
        '{"subgoals": [], "tasks": ['
        '{"id": "t1", "description": "A", "depends_on": ["t2"]}, '
        '{"id": "t2", "description": "B", "depends_on": ["t1"]}]}'
    )
    with pytest.raises(ModelOutputRejected, match="cycle"):
        parse_decomposition(text, goal_id="task_g")


async def test_malformed_success_predicate_fails_closed() -> None:
    text = (
        '{"subgoals": [], "tasks": ['
        '{"id": "t1", "description": "A", "success_predicate": {"bogus_op": []}}]}'
    )
    with pytest.raises(ModelOutputRejected, match="invalid success predicate"):
        parse_decomposition(text, goal_id="task_g")


async def test_success_predicate_is_optional_but_typed_when_present() -> None:
    ok = parse_decomposition(
        '{"subgoals": [], "tasks": [{"id": "t1", "description": "A"}]}', goal_id="task_d"
    )
    assert ok.task_graph.node("t1").success_predicate is None
    with_pred = parse_decomposition(
        '{"subgoals": [], "tasks": [{"id": "t1", "description": "A", '
        '"success_predicate": {"type": "file_exists", "path": "out.txt"}}]}',
        goal_id="task_d",
    )
    assert with_pred.task_graph.node("t1").success_predicate is not None


async def test_goal_binding_enforced_on_apply() -> None:
    decomposition = parse_decomposition(VALID, goal_id="task_other")
    state = _fresh_state()  # bound to goal task_map1
    with pytest.raises(InvalidBrainState, match="goal"):
        apply_decomposition(state, decomposition)


async def test_decomposer_refuses_already_decomposed_state(state: object) -> None:
    generator = FakeGenerator()
    decomposer = ModelDecomposer(generator)  # type: ignore[arg-type]
    with pytest.raises(InvalidTransition):
        await decomposer.decompose(state)  # type: ignore[arg-type]


async def test_decomposition_request_is_typed_and_deterministic() -> None:
    state = _fresh_state()
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, VALID)
    decomposer = ModelDecomposer(generator)  # type: ignore[arg-type]
    await decomposer.decompose(state)
    request = generator.calls[0]
    assert request.role is CognitiveRole.DECOMPOSER
    assert request.mission_id == "mission_map"
    assert request.payload["goal"]["objective"] == "Produce out.txt"
    assert request.to_canonical_json() == request.to_canonical_json()


async def test_model_output_cannot_bind_to_a_different_goal() -> None:
    """The decomposer binds output to the REQUESTED goal id only."""
    decomposition = parse_decomposition(VALID, goal_id="task_map1")
    assert decomposition.task_graph.goal_id == "task_map1"
    other = parse_decomposition(VALID, goal_id="task_other")
    assert other.task_graph.goal_id == "task_other"
    assert other.task_graph.nodes == decomposition.task_graph.nodes


def test_unsafe_ids_fail_closed() -> None:
    with pytest.raises(ModelOutputRejected, match="unsafe subgoal id"):
        parse_decomposition(
            '{"subgoals": [{"id": "bad id!", "description": "d"}], '
            '"tasks": [{"id": "t1", "description": "d", "subgoal_id": "sg1"}]}',
            goal_id="task_g",
        )
    with pytest.raises(ModelOutputRejected, match="unsafe task id"):
        parse_decomposition(
            '{"subgoals": [], "tasks": [{"id": "bad id!", "description": "d"}]}',
            goal_id="task_g",
        )


def _fresh_state() -> BrainState:
    return BrainState.for_goal(
        Goal(id="task_map1", objective="Produce out.txt"), mission_id="mission_map"
    )
