"""Sprint 6 — uncertainty: belief assessment, evidence-driven updates,
deterministic levels, and attributable world updates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.schemas import (
    MAX_BELIEF_FACTS,
    BeliefAssessment,
    FactUncertainty,
    Hypothesis,
    Prediction,
    UncertaintyLevel,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import apply_observation, assess_beliefs
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.execution import Observation, ObservationKind


def make_state(
    facts: dict[str, WorldFact] | None = None,
    hypotheses: list[Hypothesis] | None = None,
) -> BrainState:
    goal = Goal(
        id="task_g",
        objective="fix the auth module",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )
    return BrainState(
        goal=goal,
        task_graph=TaskGraph.build(goal_id="task_g"),
        world=WorldState(facts=facts or {}),
        hypotheses=hypotheses or [],
    )


def test_fact_levels_distinguish_epistemic_states() -> None:
    state = make_state(
        facts={
            "a": WorldFact(key="a", value=1, kind=WorldFactKind.VERIFIED_FACT),
            "b": WorldFact(key="b", value=2, kind=WorldFactKind.FACT),
            "c": WorldFact(key="c", kind=WorldFactKind.HYPOTHESIS),
            "d": WorldFact(key="d", kind=WorldFactKind.PREDICTION),
            "e": WorldFact(key="e", kind=WorldFactKind.UNKNOWN),
            "f": WorldFact(key="f", kind=WorldFactKind.STALE_FACT),
        }
    )
    beliefs = assess_beliefs(state)
    levels = {u.key: (u.level, u.confidence) for u in beliefs.facts}
    assert levels["a"] == (UncertaintyLevel.VERIFIED, 1.0)
    assert levels["b"] == (UncertaintyLevel.CONFIDENT, 0.8)
    assert levels["c"] == (UncertaintyLevel.UNCERTAIN, 0.4)
    assert levels["d"] == (UncertaintyLevel.UNCERTAIN, 0.4)
    assert levels["e"] == (UncertaintyLevel.UNKNOWN, 0.0)
    assert levels["f"] == (UncertaintyLevel.STALE, 0.3)


def test_conflicting_hypothesis_is_flagged() -> None:
    state = make_state(
        hypotheses=[
            Hypothesis(
                id="hyp_conflicted",
                statement="config error caused it",
                confidence=0.5,
                supporting_evidence=["obs_1"],
                conflicting_evidence=["obs_2"],
            ),
            Hypothesis(
                id="hyp_clean",
                statement="docs are stale",
                confidence=0.7,
                supporting_evidence=["obs_3"],
            ),
        ]
    )
    beliefs = assess_beliefs(state)
    assert beliefs.conflicting_hypotheses == 1
    assert beliefs.unverified_hypotheses == 1


def test_overall_uncertainty_is_deterministic_aggregate() -> None:
    unknown = make_state(facts={"x": WorldFact(key="x", kind=WorldFactKind.UNKNOWN)})
    verified = make_state(
        facts={"x": WorldFact(key="x", value=1, kind=WorldFactKind.VERIFIED_FACT)}
    )
    b_unknown = assess_beliefs(unknown)
    b_verified = assess_beliefs(verified)
    assert b_unknown.overall_uncertainty == 1.0  # nothing known
    assert b_verified.overall_uncertainty == 0.0  # fully verified
    assert b_unknown == assess_beliefs(unknown)  # deterministic


def test_uncertainty_drops_from_evidence() -> None:
    state = make_state(facts={"db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN)})
    before = assess_beliefs(state)
    observation = Observation(
        id="obs_9",
        task_id="task_g",
        kind=ObservationKind.STEP_RESULT,
        summary="queried the db",
        data={"db_status": "reachable"},
    )
    updated = apply_observation(state, observation)
    after = assess_beliefs(updated)
    assert after.overall_uncertainty < before.overall_uncertainty  # evidence lowered it
    fact = updated.world.facts["db_status"]
    assert fact.kind is WorldFactKind.FACT  # observed, NOT verified
    assert fact.value == "reachable"
    assert updated.world.version == state.world.version + 1
    assert state.world.facts["db_status"].kind is WorldFactKind.UNKNOWN  # input untouched


def test_observation_covers_only_real_data_entries() -> None:
    state = make_state(
        facts={"kept": WorldFact(key="kept", value=1, kind=WorldFactKind.VERIFIED_FACT)}
    )
    observation = Observation(
        id="obs_10",
        task_id="task_g",
        kind=ObservationKind.STEP_RESULT,
        summary="no data",
        data={},
    )
    updated = apply_observation(state, observation)
    assert updated is state  # nothing to record: identical state returned
    observation2 = Observation(
        id="obs_11",
        task_id="task_g",
        kind=ObservationKind.STEP_RESULT,
        summary="adds",
        data={"new_fact": "v", "kept": "overwritten-by-observation"},
    )
    updated2 = apply_observation(state, observation2)
    assert "new_fact" in updated2.world.facts
    assert updated2.world.facts["kept"].value == "overwritten-by-observation"


def test_predictions_stay_unverified_in_beliefs() -> None:
    from nomadicos.brain.schemas import WorldFact as WF

    prediction_world = WorldState(
        facts={"outcome": WF(key="outcome", value="ok", kind=WorldFactKind.PREDICTION)}
    )
    state = make_state().model_copy(
        update={
            "predictions": [
                Prediction(
                    id="pred_1",
                    candidate_id="cand_1",
                    predicted_world=prediction_world,
                    confidence=0.3,
                )
            ]
        }
    )
    beliefs = assess_beliefs(state)
    assert beliefs.overall_uncertainty > 0.5  # low-confidence prediction keeps it uncertain


def test_belief_assessment_is_bounded() -> None:
    facts = {
        f"k{i}": WorldFact(key=f"k{i}", kind=WorldFactKind.UNKNOWN)
        for i in range(MAX_BELIEF_FACTS + 5)
    }
    beliefs = assess_beliefs(make_state(facts=facts))
    assert len(beliefs.facts) <= MAX_BELIEF_FACTS  # bounded view
    with pytest.raises(ValidationError):
        BeliefAssessment(
            facts=[
                FactUncertainty(key=f"k{i}", level=UncertaintyLevel.UNKNOWN, confidence=0.0)
                for i in range(MAX_BELIEF_FACTS + 1)
            ],
            overall_uncertainty=1.0,
        )


def test_unknown_keys_listed_as_missing() -> None:
    state = make_state(
        facts={
            "known": WorldFact(key="known", value=1, kind=WorldFactKind.FACT),
            "missing": WorldFact(key="missing", kind=WorldFactKind.UNKNOWN),
        }
    )
    beliefs = assess_beliefs(state)
    assert beliefs.unknown_keys == ["missing"]
    assert beliefs.verified_facts == 0


def test_belief_update_is_attributable_and_replayable() -> None:
    state = make_state(facts={"k": WorldFact(key="k", kind=WorldFactKind.UNKNOWN)})
    observation = Observation(
        id="obs_x",
        task_id="task_g",
        kind=ObservationKind.STEP_RESULT,
        summary="probe",
        data={"k": "v"},
    )
    first = apply_observation(state, observation)
    second = apply_observation(state, observation)
    assert first == second  # same evidence → same belief state (replayable)
