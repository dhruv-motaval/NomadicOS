"""Actor <-> Monitor refinement loop: bounded, deterministic, provenance-
preserving, never executing (Sprint 2; NomadicBrain spec §16)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from nomadicos.brain.actor import ActorProposal, ModelActor
from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.decomposer import ModelDecomposer
from nomadicos.brain.errors import BrainError, ModelOutputRejected
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.loop import (
    ActorMonitorLoop,
    LoopConfig,
    MAPPlanner,
    PlanningOutcome,
    PlanningStage,
    PlanningStatus,
)
from nomadicos.brain.monitor import ChainedMonitor, SemanticMonitor
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.schemas import (
    ActionCandidate,
    CandidateKind,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    Subgoal,
    canonical_json,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal

from .conftest import FakeGenerator, event_id_factory

FIXED_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)

DECOMPOSITION_TEXT = (
    '{"subgoals": [{"id": "sg1", "description": "Produce out.txt"}], '
    '"tasks": [{"id": "t1", "description": "Create out.txt", "subgoal_id": "sg1", '
    '"depends_on": [], "success_predicate": {"type": "file_exists", "path": "out.txt"}}]}'
)
NO_SUBGOAL_DECOMPOSITION = (
    '{"subgoals": [], "tasks": [{"id": "t1", "description": "Create out.txt"}]}'
)
ONE_CANDIDATE = (
    '{"candidates": [{"id": "cand_ok", "kind": "EXECUTE", "tool": "terminal", '
    '"expected_effect": "out.txt exists"}]}'
)
ACCEPTING_VERDICTS = (
    '{"verdicts": [{"candidate_id": "cand_ok", "accepted": true, "reason": "aligned"}]}'
)


class ScriptedActor:
    """Deterministic Actor fake: scripted proposals per refinement pass."""

    def __init__(self, proposals: list[ActorProposal]) -> None:
        self.scripted = list(proposals)
        self.calls: list[tuple[Subgoal, list[MonitorResult], int]] = []

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ) -> ActorProposal:
        self.calls.append((subgoal, list(feedback), iteration))
        return self.scripted.pop(0)


class ScriptedMonitor:
    """Deterministic Monitor fake: scripted verdict batches; the last sticks."""

    def __init__(self, batches: list[list[MonitorResult]]) -> None:
        self.scripted = list(batches)
        self.seen: list[ActorProposal] = []

    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal: ActorProposal,
        iteration: int,
    ) -> list[MonitorResult]:
        self.seen.append(proposal)
        if len(self.scripted) > 1:
            return self.scripted.pop(0)
        return self.scripted[0]


class FailingActor:
    """Actor fake that always fails closed (model output rejected)."""

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ) -> ActorProposal:
        raise ModelOutputRejected("actor output malformed")


def _state() -> BrainState:
    return BrainState.for_goal(
        Goal(id="task_loop", objective="Produce out.txt"), mission_id="mission_loop"
    )


def _subgoal() -> Subgoal:
    return Subgoal(id="subgoal_sg1", goal_id="task_loop", description="Produce out.txt")


def _proposal(
    candidate_ids: list[str], iteration: int, derived_from: Sequence[str] = ()
) -> ActorProposal:
    candidates = [
        ActionCandidate(
            id=cid,
            subgoal_id="subgoal_sg1",
            kind=CandidateKind.OBSERVE,
            expected_effect=f"effect of {cid}",
            revision=iteration,
            derived_from=list(derived_from),
        )
        for cid in candidate_ids
    ]
    return ActorProposal(subgoal_id="subgoal_sg1", iteration=iteration, candidates=candidates)


def _rejected(candidate_id: str, iteration: int) -> MonitorResult:
    return MonitorResult(
        candidate_id=candidate_id,
        decision=MonitorDecision.REJECTED,
        stage=MonitorStage.STRUCTURAL,
        reasons=["STRUCTURAL SCHEMA: expected_effect must not be empty"],
        iteration=iteration,
    )


def _accepted(candidate_id: str, iteration: int) -> MonitorResult:
    return MonitorResult(
        candidate_id=candidate_id,
        decision=MonitorDecision.ACCEPTED,
        stage=MonitorStage.STRUCTURAL,
        iteration=iteration,
    )


def _loop(events: list[BrainEvent] | None = None, max_iterations: int = 3) -> ActorMonitorLoop:
    return ActorMonitorLoop(
        LoopConfig(max_iterations=max_iterations),
        now=lambda: FIXED_NOW,
        event_id=event_id_factory(),
        events=events,
    )


async def test_accepted_on_first_pass() -> None:
    state, subgoal = _state(), _subgoal()
    actor = ScriptedActor([_proposal(["cand_c1", "cand_c2"], 1)])
    monitor = ScriptedMonitor([[_rejected("cand_c1", 1), _accepted("cand_c2", 1)]])

    new_state, outcome = await _loop().run(state, subgoal, actor, monitor)  # type: ignore[arg-type]

    assert outcome.status is PlanningStatus.ACCEPTED
    assert outcome.iterations_used == 1
    assert outcome.accepted_candidate_ids == ["cand_c2"]
    assert new_state.planning_iterations == 1
    assert len(new_state.action_candidates) == 2
    assert [r.decision for r in new_state.monitor_results] == [
        MonitorDecision.REJECTED,
        MonitorDecision.ACCEPTED,
    ]
    assert [d.selected for d in new_state.planning_decisions] == [True]
    assert new_state.planning_decisions[0].candidate_id == "cand_c2"


async def test_rejected_then_refined_with_feedback() -> None:
    state, subgoal = _state(), _subgoal()
    second = ActorProposal(
        subgoal_id="subgoal_sg1",
        iteration=2,
        candidates=[
            ActionCandidate(
                id="cand_c2",
                subgoal_id="subgoal_sg1",
                kind=CandidateKind.EXECUTE,
                tool="terminal",
                expected_effect="out.txt exists",
                revision=2,
                derived_from=["cand_c1"],
            )
        ],
    )
    actor = ScriptedActor([_proposal(["cand_c1"], 1), second])
    monitor = ScriptedMonitor([[_rejected("cand_c1", 1)], [_accepted("cand_c2", 2)]])
    events: list[BrainEvent] = []

    new_state, outcome = await _loop(events).run(
        state,
        subgoal,
        actor,
        monitor,  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.ACCEPTED
    assert outcome.iterations_used == 2
    assert actor.calls[0][2] == 1
    assert actor.calls[1][2] == 2
    assert [r.candidate_id for r in actor.calls[1][1]] == ["cand_c1"]  # feedback carried
    assert [c.revision for c in new_state.action_candidates] == [1, 2]
    refined = new_state.action_candidates[1]
    assert refined.derived_from == ["cand_c1"]
    refinement_events = [e for e in events if e.event_type is BrainEventType.BRAIN_ACTOR_REFINEMENT]
    assert len(refinement_events) == 1


async def test_all_candidates_rejected_is_structured_failure() -> None:
    state, subgoal = _state(), _subgoal()
    actor = ScriptedActor([_proposal(["cand_c1"], 1), _proposal(["cand_c2"], 2)])
    monitor = ScriptedMonitor([[_rejected("cand_c1", 1)], [_rejected("cand_c2", 2)]])
    events: list[BrainEvent] = []

    new_state, outcome = await _loop(events, max_iterations=2).run(
        state,
        subgoal,
        actor,
        monitor,  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.FAILED
    assert outcome.accepted_candidate_ids == []
    assert outcome.failure is not None
    assert outcome.failure.stage is PlanningStage.PROPOSAL
    assert outcome.failure.iterations_used == 2
    assert "budget" in outcome.failure.reason
    assert new_state.planning_iterations == 2
    assert new_state.planning_decisions == []
    assert BrainEventType.BRAIN_PLANNING_FAILED in [e.event_type for e in events]


async def test_retry_limit_is_enforced_without_infinite_loop() -> None:
    actor = ScriptedActor(
        [_proposal(["cand_c1"], 1), _proposal(["cand_c2"], 2), _proposal(["cand_c3"], 3)]
    )
    monitor = ScriptedMonitor(
        [[_rejected("cand_c1", 1)], [_rejected("cand_c2", 2)], [_rejected("cand_c3", 3)]]
    )

    _, outcome = await ActorMonitorLoop(LoopConfig(max_iterations=2)).run(
        _state(),
        _subgoal(),
        actor,
        monitor,  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.FAILED
    assert outcome.iterations_used == 2
    assert len(actor.calls) == 2
    assert len(monitor.seen) == 2


async def test_actor_model_failure_becomes_structured_failure() -> None:
    events: list[BrainEvent] = []
    new_state, outcome = await _loop(events).run(
        _state(),
        _subgoal(),
        FailingActor(),
        ScriptedMonitor([[]]),  # type: ignore[arg-type]
    )
    assert outcome.status is PlanningStatus.FAILED
    assert outcome.failure is not None
    assert "malformed" in outcome.failure.reason
    assert BrainEventType.BRAIN_PLANNING_FAILED in [e.event_type for e in events]


async def test_deterministic_replay() -> None:
    async def run_once() -> tuple[BrainState, PlanningOutcome]:
        actor = ScriptedActor([_proposal(["cand_c1"], 1), _proposal(["cand_c2"], 2)])
        monitor = ScriptedMonitor([[_rejected("cand_c1", 1)], [_accepted("cand_c2", 2)]])
        return await _loop().run(_state(), _subgoal(), actor, monitor)  # type: ignore[arg-type]

    state_one, outcome_one = await run_once()
    state_two, outcome_two = await run_once()
    assert canonical_json(outcome_one) == canonical_json(outcome_two)
    assert state_one.to_canonical_json() == state_two.to_canonical_json()


async def test_event_trajectory_is_chained_and_typed() -> None:
    actor = ScriptedActor([_proposal(["cand_c1", "cand_c2"], 1)])
    monitor = ScriptedMonitor([[_rejected("cand_c1", 1), _accepted("cand_c2", 1)]])
    events: list[BrainEvent] = []

    await _loop(events).run(_state(), _subgoal(), actor, monitor)  # type: ignore[arg-type]

    assert [e.event_type for e in events] == [
        BrainEventType.BRAIN_CANDIDATES_GENERATED,
        BrainEventType.BRAIN_MONITOR_EVALUATED,
        BrainEventType.BRAIN_CANDIDATE_REJECTED,
        BrainEventType.BRAIN_CANDIDATE_ACCEPTED,
    ]
    for previous, current in zip(events, events[1:], strict=False):
        assert current.parent_event_id == previous.event_id
    assert all(e.mission_id == "mission_loop" for e in events)
    assert all(e.timestamp == FIXED_NOW for e in events)
    assert len({e.event_id for e in events}) == len(events)


async def test_candidates_truncated_deterministically() -> None:
    actor = ScriptedActor([_proposal(["cand_c1", "cand_c2"], 1)])
    monitor = ScriptedMonitor([[_accepted("cand_c1", 1)]])

    _, outcome = await ActorMonitorLoop(LoopConfig(max_candidates=1)).run(
        _state(),
        _subgoal(),
        actor,
        monitor,  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.ACCEPTED
    assert len(monitor.seen[0].candidates) == 1  # second candidate truncated


async def test_subgoal_goal_mismatch_fails_closed() -> None:
    other = Subgoal(id="subgoal_x", goal_id="task_other", description="d")
    with pytest.raises(BrainError, match="goal"):
        await _loop().run(
            _state(),
            other,
            ScriptedActor([]),
            ScriptedMonitor([]),  # type: ignore[arg-type]
        )


# ------------------------------------------------------------ MAP facade ---


async def test_plan_full_map_run() -> None:
    state = BrainState.for_goal(
        Goal(id="task_map", objective="Produce out.txt"), mission_id="mission_map"
    )
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, DECOMPOSITION_TEXT)
    generator.script(CognitiveRole.ACTOR, ONE_CANDIDATE)
    generator.script(CognitiveRole.MONITOR, ACCEPTING_VERDICTS)
    events: list[BrainEvent] = []
    planner = MAPPlanner(
        ActorMonitorLoop(now=lambda: FIXED_NOW, event_id=event_id_factory(), events=events)
    )

    planned, outcome = await planner.plan(
        state,  # type: ignore[arg-type]
        ModelDecomposer(generator),  # type: ignore[arg-type]
        ModelActor(generator),  # type: ignore[arg-type]
        ChainedMonitor(semantic=SemanticMonitor(generator)),  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.ACCEPTED
    assert outcome.accepted_candidate_ids == ["cand_ok"]
    assert planned.task_graph.node_ids() == ["t1"]
    assert planned.task_graph.subgoals[0].id == "sg1"
    assert len(planned.action_candidates) == 1
    event_types = [e.event_type for e in events]
    assert event_types[:2] == [
        BrainEventType.BRAIN_DECOMPOSITION_REQUESTED,
        BrainEventType.BRAIN_DECOMPOSITION_PRODUCED,
    ]
    assert BrainEventType.BRAIN_CANDIDATE_ACCEPTED in event_types
    assert BrainEventType.BRAIN_DECOMPOSITION_FAILED not in event_types


async def test_plan_decomposition_failure_is_structured() -> None:
    state = BrainState.for_goal(
        Goal(id="task_map2", objective="Produce out.txt"), mission_id="mission_map2"
    )
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, "definitely not json")
    events: list[BrainEvent] = []
    loop = ActorMonitorLoop(now=lambda: FIXED_NOW, event_id=event_id_factory(), events=events)

    planned, outcome = await MAPPlanner(loop).plan(
        state,  # type: ignore[arg-type]
        ModelDecomposer(generator),  # type: ignore[arg-type]
        ScriptedActor([]),  # type: ignore[arg-type]
        ScriptedMonitor([[]]),  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.stage is PlanningStage.DECOMPOSITION
    assert planned.task_graph.nodes == []  # nothing entered the graph
    event_types = [e.event_type for e in events]
    assert BrainEventType.BRAIN_DECOMPOSITION_FAILED in event_types
    assert BrainEventType.BRAIN_PLANNING_FAILED in event_types


async def test_plan_without_subgoal_fails_explicitly() -> None:
    state = BrainState.for_goal(
        Goal(id="task_map3", objective="Produce out.txt"), mission_id="mission_map3"
    )
    generator = FakeGenerator()
    generator.script(CognitiveRole.DECOMPOSER, NO_SUBGOAL_DECOMPOSITION)

    planned, outcome = await MAPPlanner(
        loop=ActorMonitorLoop(now=lambda: FIXED_NOW, event_id=event_id_factory())
    ).plan(
        state,  # type: ignore[arg-type]
        ModelDecomposer(generator),  # type: ignore[arg-type]
        ScriptedActor([]),  # type: ignore[arg-type]
        ScriptedMonitor([[]]),  # type: ignore[arg-type]
    )

    assert outcome.status is PlanningStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.stage is PlanningStage.PROPOSAL
    assert "subgoal" in outcome.failure.reason


async def test_plan_on_decomposed_state_skips_decomposition() -> None:
    goal = Goal(id="task_map4", objective="Produce out.txt")
    subgoal = Subgoal(id="subgoal_sg1", goal_id="task_map4", description="Produce out.txt")
    graph = TaskGraph.build(
        goal_id=goal.id,
        nodes=[TaskNode(id="t1", description="Create out.txt", subgoal_id="subgoal_sg1")],
        subgoals=[subgoal],
    )
    state = BrainState.for_goal(goal, task_graph=graph, mission_id="mission_map4")
    actor = ScriptedActor([_proposal(["cand_c1"], 1)])
    monitor = ScriptedMonitor([[_accepted("cand_c1", 1)]])
    events: list[BrainEvent] = []
    loop = ActorMonitorLoop(now=lambda: FIXED_NOW, event_id=event_id_factory(), events=events)

    planned, outcome = await MAPPlanner(loop).plan(
        state,  # type: ignore[arg-type]
        ModelDecomposer(FakeGenerator()),  # type: ignore[arg-type]
        actor,  # type: ignore[arg-type]
        monitor,  # type: ignore[arg-type]
        subgoal=subgoal,
    )

    event_types = [e.event_type for e in events]
    assert BrainEventType.BRAIN_DECOMPOSITION_REQUESTED not in event_types
    assert outcome.status is PlanningStatus.ACCEPTED
    assert planned.action_candidates[0].id == "cand_c1"
