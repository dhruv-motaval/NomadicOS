"""Monitor: deterministic structural validation, model-backed semantic
checks, fail-closed semantics, candidate identity (Sprint 2; spec §15)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.actor import ActorProposal
from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.monitor import ChainedMonitor, SemanticMonitor, StructuralMonitor
from nomadicos.brain.schemas import (
    ActionCandidate,
    CandidateKind,
    Capability,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    Subgoal,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal

from .conftest import FakeGenerator


def _state(*capability_names: str) -> BrainState:
    state = BrainState.for_goal(
        Goal(id="task_mon", objective="Produce out.txt"), mission_id="mission_monitor"
    )
    if capability_names:
        state = state.model_copy(
            update={"capabilities": [Capability(name=n) for n in capability_names]}
        )
    return state


def _subgoal() -> Subgoal:
    return Subgoal(id="subgoal_sg1", goal_id="task_mon", description="Produce out.txt")


def _proposal(*candidates: ActionCandidate) -> ActorProposal:
    return ActorProposal(subgoal_id="subgoal_sg1", iteration=1, candidates=list(candidates))


def _candidate(**overrides: object) -> ActionCandidate:
    base: dict[str, object] = {
        "id": "cand_c1",
        "subgoal_id": "subgoal_sg1",
        "kind": CandidateKind.OBSERVE,
        "expected_effect": "learn the directory layout",
    }
    base.update(**overrides)  # type: ignore[arg-type]
    return ActionCandidate(**base)  # type: ignore[arg-type]


async def test_structural_accepts_valid_candidates() -> None:
    monitor = StructuralMonitor()
    proposal = _proposal(
        ActionCandidate(
            id="cand_ok1",
            subgoal_id="subgoal_sg1",
            kind=CandidateKind.OBSERVE,
            expected_effect="learn",
        ),
        ActionCandidate(
            id="cand_ok2",
            subgoal_id="subgoal_sg1",
            kind=CandidateKind.EXECUTE,
            tool="terminal",
            expected_effect="out.txt exists",
        ),
    )
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert all(r.decision is MonitorDecision.ACCEPTED for r in results)
    assert all(r.stage is MonitorStage.STRUCTURAL for r in results)
    assert [r.candidate_id for r in results] == ["cand_ok1", "cand_ok2"]


async def test_structural_rejects_without_any_model() -> None:
    """The deterministic layer rejects malformed candidates with no model."""
    monitor = StructuralMonitor()
    proposal = _proposal(
        ActionCandidate(
            id="bad id!", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e"
        ),
        ActionCandidate(
            id="cand_ok2", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect=""
        ),
    )
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert [r.decision for r in results] == [MonitorDecision.REJECTED, MonitorDecision.REJECTED]
    assert results[0].stage is MonitorStage.STRUCTURAL
    assert "SCHEMA" in results[0].reasons[0]
    assert results[1].reasons  # explicit reason, no silent drop


async def test_structural_rejects_subgoal_binding_drift() -> None:
    monitor = StructuralMonitor()
    candidate = _candidate()  # consistent construction
    proposal = _proposal(candidate)
    # model_copy bypasses ActorProposal validators; the monitor must catch it
    tampered = proposal.model_copy(
        update={"candidates": [candidate.model_copy(update={"subgoal_id": "subgoal_other"})]}
    )
    results = await monitor.evaluate(_state(), _subgoal(), tampered, 1)  # type: ignore[arg-type]
    assert results[0].decision is MonitorDecision.REJECTED
    assert "BINDING" in results[0].reasons[0]


async def test_structural_rejects_unknown_capability() -> None:
    monitor = StructuralMonitor()
    candidate = ActionCandidate(
        id="cand_cap",
        subgoal_id="subgoal_sg1",
        kind=CandidateKind.EXECUTE,
        capability="filesystem",
        expected_effect="file written",
    )
    proposal = _proposal(candidate)
    # no declared capabilities -> nothing to check against; structurally accepted
    undetermined = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert undetermined[0].decision is MonitorDecision.ACCEPTED
    # declared capabilities make existence checkable: unknown ones are rejected
    declared = _state("terminal")
    rejected = await monitor.evaluate(declared, _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert rejected[0].decision is MonitorDecision.REJECTED
    assert rejected[0].reasons[0].startswith("STRUCTURAL CAPABILITY")


async def test_structural_rejects_unserializable_arguments() -> None:
    monitor = StructuralMonitor()
    candidate = ActionCandidate(
        id="cand_x",
        subgoal_id="subgoal_sg1",
        kind=CandidateKind.OBSERVE,
        arguments={"fn": object()},
        expected_effect="e",
    )
    proposal = _proposal(candidate)
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert results[0].decision is MonitorDecision.REJECTED
    assert "SCHEMA" in results[0].reasons[0]


async def test_semantic_acceptance_and_rejection() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.MONITOR,
        '{"verdicts": ['
        '{"candidate_id": "cand_c1", "accepted": true, "reason": "aligned with subgoal"}, '
        '{"candidate_id": "cand_c2", "accepted": false, "reason": "irrelevant to subgoal"}]}',
    )
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_c1", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e1"
        ),
        ActionCandidate(
            id="cand_c2", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e2"
        ),
    )
    state, subgoal = _state(), _subgoal()
    results = await monitor.evaluate(state, subgoal, proposal, 1)  # type: ignore[arg-type]

    assert [r.decision for r in results] == [MonitorDecision.ACCEPTED, MonitorDecision.REJECTED]
    assert results[0].stage is MonitorStage.SEMANTIC
    assert results[1].reasons == ["SEMANTIC: irrelevant to subgoal"]
    assert results[0].reasons == ["aligned with subgoal"]
    assert generator.calls[0].role is CognitiveRole.MONITOR


async def test_malformed_semantic_output_rejects_everything() -> None:
    generator = FakeGenerator()
    generator.script(CognitiveRole.MONITOR, "this is not json")
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_ok", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e"
        )
    )
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert results[0].decision is MonitorDecision.REJECTED
    assert results[0].stage is MonitorStage.SEMANTIC
    assert "SEMANTIC MALFORMED" in results[0].reasons[0]


async def test_missing_verdict_is_rejected_not_approved() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.MONITOR,
        '{"verdicts": [{"candidate_id": "cand_c1", "accepted": true, "reason": "ok"}]}',
    )
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_c1", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e1"
        ),
        ActionCandidate(
            id="cand_c2", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e2"
        ),
    )
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    verdicts = {r.candidate_id: r.decision for r in results}
    assert verdicts == {"cand_c1": MonitorDecision.ACCEPTED, "cand_c2": MonitorDecision.REJECTED}


async def test_unknown_verdict_references_are_dropped() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.MONITOR,
        '{"verdicts": [{"candidate_id": "cand_ghost", "accepted": true, "reason": "x"}]}',
    )
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_real",
            subgoal_id="subgoal_sg1",
            kind=CandidateKind.OBSERVE,
            expected_effect="e",
        )
    )
    results = await monitor.evaluate(_state(), _subgoal(), proposal, 1)  # type: ignore[arg-type]
    assert results[0].decision is MonitorDecision.REJECTED
    assert "no verdict" in results[0].reasons[0]


async def test_structural_short_circuits_semantic_stage() -> None:
    generator = FakeGenerator()
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_bad", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e"
        )
    )
    # bypass ActorProposal validators to simulate a tampered subgoal binding
    tampered = proposal.model_copy(
        update={
            "candidates": [
                proposal.candidates[0].model_copy(update={"subgoal_id": "subgoal_other"})
            ]
        }
    )
    results = await monitor.evaluate(_state(), _subgoal(), tampered, 1)  # type: ignore[arg-type]
    assert results[0].stage is MonitorStage.STRUCTURAL
    assert generator.calls == []  # the model was never consulted


def test_rejected_monitor_result_requires_explicit_reason() -> None:
    with pytest.raises(ValidationError):
        MonitorResult(
            candidate_id="cand_c1",
            decision=MonitorDecision.REJECTED,
            stage=MonitorStage.SEMANTIC,
            reasons=[],
        )


def test_monitor_result_rejects_authority_fields() -> None:
    with pytest.raises(ValidationError):
        MonitorResult(
            candidate_id="cand_c1",
            decision=MonitorDecision.ACCEPTED,
            stage=MonitorStage.SEMANTIC,
            authorized=True,  # type: ignore[call-overload]
        )


async def test_structural_only_monitor_accepts_valid_candidates() -> None:
    monitor = ChainedMonitor()  # no semantic layer: deterministic checks only
    proposal = _proposal(
        ActionCandidate(
            id="cand_c1", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e"
        )
    )
    state, subgoal = _state(), _subgoal()
    results = await monitor.evaluate(state, subgoal, proposal, 1)  # type: ignore[arg-type]
    assert [r.decision for r in results] == [MonitorDecision.ACCEPTED]


async def test_semantic_result_references_candidate_identity() -> None:
    generator = FakeGenerator()
    generator.script(
        CognitiveRole.MONITOR,
        '{"verdicts": [{"candidate_id": "cand_c1", "accepted": true, "reason": "ok"}]}',
    )
    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    proposal = _proposal(
        ActionCandidate(
            id="cand_c1", subgoal_id="subgoal_sg1", kind=CandidateKind.OBSERVE, expected_effect="e"
        )
    )
    state, subgoal = _state(), _subgoal()
    results = await monitor.evaluate(state, subgoal, proposal, 2)  # type: ignore[arg-type]
    assert results[0].candidate_id == "cand_c1"
    assert results[0].iteration == 2
    assert generator.calls[0].payload["candidates"][0]["id"] == "cand_c1"
