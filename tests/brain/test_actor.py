"""Actor: multiple typed candidates, provenance, fail-closed validation,
no execution/authority side effects (Sprint 2; NomadicBrain spec §14)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.actor import ActorProposal, ModelActor
from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.errors import InvalidBrainState, ModelOutputRejected
from nomadicos.brain.schemas import (
    ActionCandidate,
    CandidateKind,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    Subgoal,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal

from .conftest import FakeGenerator

THREE_CANDIDATES = (
    '{"candidates": ['
    '{"id": "c1", "kind": "EXECUTE", "tool": "terminal", '
    '"arguments": {"command": "echo hi"}, "preconditions": ["workspace exists"], '
    '"expected_effect": "out.txt exists", "rationale": "fastest"}, '
    '{"id": "c2", "kind": "OBSERVE", "expected_effect": "learn directory layout"}, '
    '{"id": "c3", "kind": "QUERY", "capability": "ask_owner", '
    '"expected_effect": "clarified constraint", "rationale": "ambiguity"}]}'
)

AUTHORITY_FIELDS = frozenset({"authorized", "allowed", "permission", "bypass", "grants"})


def _state() -> BrainState:
    return BrainState.for_goal(
        Goal(id="task_a1", objective="Produce out.txt"), mission_id="mission_actor"
    )


def _subgoal() -> Subgoal:
    return Subgoal(id="subgoal_sg1", goal_id="task_a1", description="Produce out.txt")


def _rejection(candidate_id: str) -> MonitorResult:
    return MonitorResult(
        candidate_id=candidate_id,
        decision=MonitorDecision.REJECTED,
        stage=MonitorStage.STRUCTURAL,
        reasons=["STRUCTURAL SCHEMA: expected_effect must not be empty"],
        iteration=1,
    )


async def test_actor_generates_multiple_typed_candidates() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES)
    actor = ModelActor(generator)  # type: ignore[arg-type]
    subgoal = _subgoal()
    proposal = await actor.propose(_state(), subgoal)  # type: ignore[arg-type]

    assert len(proposal.candidates) == 3
    first = proposal.candidates[0]
    assert first.kind is CandidateKind.EXECUTE
    assert first.tool == "terminal"
    assert first.arguments == {"command": "echo hi"}
    assert first.preconditions == ["workspace exists"]
    assert first.expected_effect == "out.txt exists"
    assert first.rationale == "fastest"
    assert first.subgoal_id == subgoal.id
    assert proposal.subgoal_id == subgoal.id
    assert proposal.iteration == 1
    assert proposal.candidates[1].kind is CandidateKind.OBSERVE
    assert proposal.candidates[2].kind is CandidateKind.QUERY


async def test_actor_proposal_is_deterministic() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES, THREE_CANDIDATES)
    actor = ModelActor(generator)  # type: ignore[arg-type]
    subgoal = _subgoal()
    first = await actor.propose(_state(), subgoal)  # type: ignore[arg-type]
    second = await actor.propose(_state(), subgoal)  # type: ignore[arg-type]
    assert first == second


async def test_invalid_candidate_output_fails_closed() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.ACTOR,
        '{"candidates": [{"id": "c1", "kind": "MURDER", "expected_effect": "x"}]}',
        '{"candidates": [{"id": "bad id!", "kind": "OBSERVE", "expected_effect": "e"}]}',
        '{"candidates": [{"id": "c1", "kind": "EXECUTE", "expected_effect": ""}]}',
        '{"candidates": []}',
    )
    actor = ModelActor(generator)  # type: ignore[arg-type]
    state, subgoal = _state(), _subgoal()
    for _ in range(4):
        with pytest.raises(ModelOutputRejected):
            await actor.propose(state, subgoal)  # type: ignore[arg-type]


async def test_refinement_provenance_from_feedback() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES)
    actor = ModelActor(generator)  # type: ignore[arg-type]
    subgoal = _subgoal()
    feedback = (_rejection("cand_old1"), _rejection("cand_old2"))
    proposal = await actor.propose(  # type: ignore[arg-type]
        _state(), subgoal, feedback=feedback, iteration=2
    )

    assert proposal.iteration == 2
    for candidate in proposal.candidates:
        assert candidate.revision == 2
        assert "cand_old1" in candidate.derived_from
        assert "cand_old2" in candidate.derived_from


def test_actor_proposal_rejects_authority_fields() -> None:
    with pytest.raises(ValidationError):
        ActorProposal(  # type: ignore[call-overload]
            subgoal_id="subgoal_sg",
            iteration=1,
            candidates=[
                ActionCandidate(
                    id="cand_c1",
                    kind=CandidateKind.EXECUTE,
                    tool="terminal",
                    subgoal_id="subgoal_sg",
                    authorized=True,  # type: ignore[call-overload]
                )
            ],
        )


async def test_actor_never_produces_authority_or_execution_artifacts() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES)
    proposal = await ModelActor(generator).propose(_state(), _subgoal())  # type: ignore[arg-type]
    assert type(proposal) is ActorProposal
    candidate_fields = set(ActionCandidate.model_fields)
    assert not (AUTHORITY_FIELDS & candidate_fields)
    for candidate in proposal.candidates:
        assert type(candidate) is ActionCandidate
        assert candidate.model_extra is None


async def test_actor_subgoal_binding_enforced() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES)
    actor = ModelActor(generator)  # type: ignore[arg-type]
    wrong_goal_subgoal = Subgoal(id="subgoal_x", goal_id="task_other", description="d")
    with pytest.raises(InvalidBrainState, match="goal"):
        await actor.propose(_state(), wrong_goal_subgoal)  # type: ignore[arg-type]


async def test_actor_rejects_mismatched_subgoal_reference_in_output() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.ACTOR,
        '{"candidates": [{"id": "c1", "kind": "OBSERVE", "subgoal_id": "subgoal_other", '
        '"expected_effect": "e"}]}',
    )
    actor = ModelActor(generator)  # type: ignore[arg-type]
    with pytest.raises(ModelOutputRejected, match="subgoal"):
        await actor.propose(_state(), _subgoal())  # type: ignore[arg-type]


async def test_actor_records_request_provenance() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.ACTOR, THREE_CANDIDATES)
    actor = ModelActor(generator)  # type: ignore[arg-type]
    subgoal = _subgoal()
    await actor.propose(_state(), subgoal, iteration=1)  # type: ignore[arg-type]
    request = generator.calls[0]
    assert request.role is CognitiveRole.ACTOR
    assert request.mission_id == "mission_actor"
    assert request.payload["subgoal"]["id"] == subgoal.id
