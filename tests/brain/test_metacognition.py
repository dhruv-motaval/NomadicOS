"""Sprint 6 — metacognition (advisory assessment) and information
gathering (bounded requests, existing-pipeline bridge)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.information import identify_information_needs, to_observe_candidate
from nomadicos.brain.metacognition import assess_metacognition
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.schemas import (
    MAX_INFO_REQUESTS,
    ActionCandidate,
    BudgetState,
    CandidateKind,
    Hypothesis,
    MemoryRef,
    MetacognitiveRecommendation,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import assess_beliefs
from nomadicos.contracts.core import Goal, parse_predicate


def make_state(
    *,
    facts: dict[str, WorldFact] | None = None,
    hypotheses: list[Hypothesis] | None = None,
    budget: BudgetState | None = None,
    candidates: list[ActionCandidate] | None = None,
    memories: list[MemoryRef] | None = None,
) -> BrainState:
    goal = Goal(
        id="task_g",
        objective="fix the auth module",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )
    sub = Subgoal(id="subgoal_s1", goal_id="task_g", description="write failing auth test")
    graph = TaskGraph.build(goal_id="task_g", subgoals=[sub])
    return BrainState(
        goal=goal,
        task_graph=graph,
        active_subgoal=sub,
        world=WorldState(facts=facts or {}),
        hypotheses=hypotheses or [],
        budget=budget or BudgetState(),
        action_candidates=candidates or [],
        relevant_memories=memories or [],
    )


# ------------------------------------------------------- metacognition ---


def test_metacognition_answers_the_five_questions() -> None:
    state = make_state(
        facts={"db_status": WorldFact(key="db_status", value="ok", kind=WorldFactKind.FACT)},
        hypotheses=[
            Hypothesis(
                id="hyp_1", statement="config error", confidence=0.6,
                supporting_evidence=["obs_1"],
            )
        ],
        memories=[MemoryRef(memory_id="mem_auth", score=0.9)],
    )
    assessment = assess_metacognition(state)
    assert any("db_status" in b for b in assessment.believed)  # what we believe
    assert any("hyp_1" in b for b in assessment.believed)
    assert "obs_1" in assessment.evidence_refs or "mem_auth" in assessment.evidence_refs
    assert assessment.recommendation in MetacognitiveRecommendation
    assert assessment.reasons


def test_metacognition_flags_uncertainty_and_missing() -> None:
    state = make_state(
        facts={
            "db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN),
            "stale_cfg": WorldFact(key="stale_cfg", kind=WorldFactKind.STALE_FACT),
        },
        hypotheses=[
            Hypothesis(id="hyp_weak", statement="maybe cache", confidence=0.2,
                       supporting_evidence=[], conflicting_evidence=[])
        ],
    )
    assessment = assess_metacognition(state)
    assert any("db_status" in u for u in assessment.uncertainties)
    assert any("hyp_weak" in u for u in assessment.uncertainties)
    assert "db_status" in assessment.missing


def test_recommendation_gather_info_when_unknowns() -> None:
    state = make_state(facts={"x": WorldFact(key="x", kind=WorldFactKind.UNKNOWN)})
    assessment = assess_metacognition(state)
    assert assessment.recommendation is MetacognitiveRecommendation.GATHER_INFO
    assert assessment.continue_reasoning is True


def test_recommendation_stop_when_budget_exhausted() -> None:
    state = make_state(budget=BudgetState(max_steps=5, steps_used=5))
    assessment = assess_metacognition(state)
    assert assessment.recommendation is MetacognitiveRecommendation.STOP
    assert assessment.continue_reasoning is False


def test_recommendation_continue_when_settled() -> None:
    state = make_state(
        facts={"db_status": WorldFact(key="db_status", value=1, kind=WorldFactKind.VERIFIED_FACT)}
    )
    assessment = assess_metacognition(state)
    assert assessment.recommendation is MetacognitiveRecommendation.CONTINUE


def test_metacognition_is_deterministic() -> None:
    state = make_state(facts={"x": WorldFact(key="x", kind=WorldFactKind.UNKNOWN)})
    assert assess_metacognition(state) == assess_metacognition(state)


def test_metacognition_is_advisory_data_only() -> None:
    # closed schema: authority/execution fields are structurally impossible
    assessment = assess_metacognition(make_state())
    dump = assessment.model_dump(mode="json")
    with pytest.raises(ValidationError):
        type(assessment).model_validate({**dump, "authorized": True})
    with pytest.raises(ValidationError):
        type(assessment).model_validate(
            {"continue_reasoning": True, "recommendation": "CONTINUE", "execute": True}
        )


def test_metacognition_is_bounded() -> None:
    state = make_state(
        facts={f"unknown_{i}": WorldFact(key=f"unknown_{i}", kind=WorldFactKind.UNKNOWN)
               for i in range(20)}
    )
    assessment = assess_metacognition(state)
    assert len(assessment.missing) <= 8  # MAX_METACOGNITION_ITEMS
    assert len(assessment.believed) <= 8


# ---------------------------------------------------- information needs ---


def test_information_requests_from_unknown_facts() -> None:
    state = make_state(
        facts={
            "db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN),
            "cache_mode": WorldFact(key="cache_mode", kind=WorldFactKind.UNKNOWN),
        }
    )
    beliefs = assess_beliefs(state)
    requests = identify_information_needs(state, beliefs)
    assert [r.target for r in requests] == ["cache_mode", "db_status"]  # deterministic order
    assert all(r.priority == 1.0 for r in requests)


def test_information_requests_from_unevidenced_hypotheses() -> None:
    state = make_state(
        hypotheses=[
            Hypothesis(id="hyp_no_ev", statement="cache issue", confidence=0.5),
            Hypothesis(id="hyp_ev", statement="config issue", confidence=0.5,
                       supporting_evidence=["obs_1"]),
        ]
    )
    requests = identify_information_needs(state)
    targets = [r.target for r in requests]
    assert targets == ["hyp_no_ev"]  # evidenced hypothesis needs nothing
    assert requests[0].priority < 1.0


def test_information_requests_are_bounded() -> None:
    state = make_state(
        facts={f"u{i}": WorldFact(key=f"u{i}", kind=WorldFactKind.UNKNOWN) for i in range(20)}
    )
    requests = identify_information_needs(state)
    assert len(requests) == MAX_INFO_REQUESTS  # hard bound


def test_information_requests_respect_budget() -> None:
    state = make_state(
        facts={"x": WorldFact(key="x", kind=WorldFactKind.UNKNOWN)},
        budget=BudgetState(max_steps=3, steps_used=3),
    )
    assert identify_information_needs(state) == []  # exhausted budget: no gathering


def test_observe_candidate_travels_existing_pipeline() -> None:
    state = make_state(facts={"db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN)})
    request = identify_information_needs(state)[0]
    candidate = to_observe_candidate(request, subgoal_id="subgoal_s1")
    assert candidate.kind is CandidateKind.OBSERVE  # observe, not execute
    assert candidate.subgoal_id == "subgoal_s1"
    assert candidate.id.startswith("cand_")
    # it is a NORMAL candidate: same closed schema, no authority fields
    with pytest.raises(ValidationError):
        ActionCandidate.model_validate({**candidate.model_dump(mode="json"), "authorized": True})
    # deterministic id: same request → same candidate
    assert candidate.id == to_observe_candidate(request, subgoal_id="subgoal_s1").id
