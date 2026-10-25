"""BrainState: construction, determinism, serialization, coherence
(Sprint 1; NomadicBrain spec §5-6)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.errors import InvalidBrainState
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.schemas import Subgoal
from nomadicos.brain.state import BRAIN_STATE_SCHEMA_VERSION, BrainState
from nomadicos.contracts.core import Goal
from nomadicos.contracts.core import Goal as CanonicalGoal


def test_for_goal_minimal_construction_defaults(goal: Goal) -> None:
    state = BrainState.for_goal(goal, mission_id="mission_min")
    assert state.mission_id == "mission_min"
    assert state.schema_version == BRAIN_STATE_SCHEMA_VERSION
    assert state.state_version == 1
    assert state.goal is goal
    assert state.task_graph.goal_id == goal.id
    assert state.task_graph.nodes == []  # not yet decomposed
    assert state.active_subgoal is None
    assert state.world.version == 0 and state.world.facts == {}
    assert state.working_memory.notes == []
    assert state.relevant_memories == [] and state.hypotheses == []
    assert state.observations == [] and state.evidence == []
    assert state.capabilities == [] and state.history == []
    assert state.budget.steps_used == 0 and state.budget.max_steps == 32


def test_for_goal_with_explicit_graph(goal: Goal, chain: TaskGraph) -> None:
    state = BrainState.for_goal(goal, task_graph=chain)
    assert state.task_graph is chain
    assert [n.id for n in state.task_graph.nodes] == ["a", "b", "c"]


def test_construction_rejects_extra_authority_shaped_fields(goal: Goal, chain: TaskGraph) -> None:
    with pytest.raises(ValidationError):
        BrainState(  # type: ignore[call-overload]
            goal=goal,
            task_graph=chain,
            mission_id="mission_x",
            authorized=True,
        )


def test_empty_mission_id_rejected(goal: Goal, chain: TaskGraph) -> None:
    with pytest.raises(ValidationError):
        BrainState(goal=goal, task_graph=chain, mission_id="   ")


def test_schema_version_fails_closed(goal: Goal, chain: TaskGraph) -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        BrainState(
            goal=goal,
            task_graph=chain,
            mission_id="mission_v2",
            schema_version=BRAIN_STATE_SCHEMA_VERSION + 1,
        )


def test_graph_must_belong_to_the_state_goal(goal: Goal) -> None:
    other = TaskGraph.build(goal_id="task_other", nodes=[TaskNode(id="a", description="A")])
    with pytest.raises(ValidationError, match="belongs to goal"):
        BrainState(goal=goal, task_graph=other, mission_id="mission_mismatch")


def test_active_subgoal_must_belong_to_goal_and_graph(goal: Goal) -> None:
    subgoal = Subgoal(id="subgoal_sg", goal_id=goal.id, description="Prepare environment")
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[TaskNode(id="a", description="A", subgoal_id="subgoal_sg")],
        subgoals=[subgoal],
    )
    with pytest.raises(ValidationError, match="not part of"):
        BrainState(
            goal=goal,
            task_graph=graph,
            mission_id="mission_sg",
            active_subgoal=Subgoal(id="subgoal_other", goal_id=goal.id, description="elsewhere"),
        )
    with pytest.raises(ValidationError, match="does not belong"):
        BrainState(
            goal=goal,
            task_graph=graph,
            mission_id="mission_sg",
            active_subgoal=Subgoal(id="subgoal_sg", goal_id="task_other", description="x"),
        )


def test_state_is_frozen(goal: Goal, chain: TaskGraph) -> None:
    state = BrainState.for_goal(goal, task_graph=chain, mission_id="mission_frozen")
    with pytest.raises(ValidationError):
        state.state_version = 99  # type: ignore[misc]


def test_deterministic_equality_and_canonical_json(goal: Goal, chain: TaskGraph) -> None:
    first = BrainState.for_goal(goal, task_graph=chain, mission_id="mission_det")
    second = BrainState.for_goal(goal, task_graph=chain, mission_id="mission_det")
    assert first == second
    assert first.to_canonical_json() == second.to_canonical_json()
    assert first.to_canonical_json() == first.to_canonical_json()


def test_serialization_round_trip(goal: Goal, chain: TaskGraph) -> None:
    state = BrainState.for_goal(goal, task_graph=chain, mission_id="mission_rt")
    assert BrainState.model_validate_json(state.model_dump_json()) == state
    assert BrainState.model_validate_json(state.to_canonical_json()) == state
    again = BrainState.model_validate_json(state.to_canonical_json())
    assert again.to_canonical_json() == state.to_canonical_json()


def test_deserialization_rejects_unknown_schema_version(goal: Goal, chain: TaskGraph) -> None:
    state = BrainState.for_goal(goal, task_graph=chain, mission_id="mission_load")
    payload = state.model_dump(mode="json")
    payload["schema_version"] = 99
    with pytest.raises(ValidationError, match="schema_version"):
        BrainState.model_validate(payload)


def test_with_event_appends_reference_and_bumps_version(state: BrainState) -> None:
    event = BrainEvent(
        event_id="evt_1",
        mission_id=state.mission_id,
        event_type=BrainEventType.BRAIN_STATE_CREATED,
        payload={"goal": state.goal.objective},
    )
    updated = state.with_event(event)
    assert [ref.event_id for ref in updated.history] == ["evt_1"]
    assert updated.state_version == state.state_version + 1
    assert state.history == []  # original state is untouched (immutability)
    assert updated.goal == state.goal and updated.task_graph == state.task_graph


def test_with_event_chains_deterministically(state: BrainState) -> None:
    event_a = BrainEvent(
        event_id="evt_a", mission_id=state.mission_id, event_type=BrainEventType.BRAIN_TASK_STARTED
    )
    event_b = BrainEvent(
        event_id="evt_b",
        mission_id=state.mission_id,
        event_type=BrainEventType.BRAIN_TASK_COMPLETED,
        parent_event_id="evt_a",
    )
    updated = state.with_event(event_a).with_event(event_b)
    assert [ref.event_id for ref in updated.history] == ["evt_a", "evt_b"]
    assert updated.state_version == state.state_version + 2


def test_with_event_rejects_foreign_mission(state: BrainState) -> None:
    foreign = BrainEvent(
        event_id="evt_f",
        mission_id="mission_other",
        event_type=BrainEventType.BRAIN_TASK_STARTED,
    )
    with pytest.raises(InvalidBrainState):
        state.with_event(foreign)


def test_brain_state_reuses_existing_goal_contract() -> None:
    """BrainState.goal IS the NomadicOS Goal contract — no duplicate."""
    assert BrainState.model_fields["goal"].annotation is CanonicalGoal
