"""Uncertainty — explicit belief-state assessment and attributable belief
updates (Sprint 6, spec §8).

Uncertain reality is NEVER reduced to a single asserted state: every
world fact keeps its epistemic kind (FACT / UNKNOWN / HYPOTHESIS /
PREDICTION / VERIFIED_FACT / STALE_FACT), and ``assess_beliefs`` derives
a deterministic uncertainty view from those kinds, hypothesis evidence,
and prediction confidence:

    level          confidence   source
    VERIFIED       1.0          VERIFIED_FACT kind
    CONFIDENT      0.8          plain observed FACT
    UNCERTAIN      0.4          HYPOTHESIS / PREDICTION kind
    CONFLICTING    0.3          hypothesis with BOTH supporting and
                                conflicting evidence
    UNKNOWN        0.0          UNKNOWN kind
    STALE          0.3          STALE_FACT kind

``apply_observation`` is the ONLY belief-update path here: entries of a
real Observation's data dict become plain FACT-kind world facts —
attributable to the observation (its id is the deterministic fact
source), bounded, and NEVER auto-verified (verification stays with the
existing verifier boundary). Unknown facts covered by the observation
move from UNKNOWN to FACT — uncertainty drops deterministically from
evidence. DATA only: no authority, no execution, no verification.
"""

from __future__ import annotations

from nomadicos.brain.schemas import (
    MAX_BELIEF_FACTS,
    BeliefAssessment,
    FactUncertainty,
    UncertaintyLevel,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.execution import Observation

_LEVELS: dict[WorldFactKind, tuple[UncertaintyLevel, float]] = {
    WorldFactKind.VERIFIED_FACT: (UncertaintyLevel.VERIFIED, 1.0),
    WorldFactKind.FACT: (UncertaintyLevel.CONFIDENT, 0.8),
    WorldFactKind.HYPOTHESIS: (UncertaintyLevel.UNCERTAIN, 0.4),
    WorldFactKind.PREDICTION: (UncertaintyLevel.UNCERTAIN, 0.4),
    WorldFactKind.UNKNOWN: (UncertaintyLevel.UNKNOWN, 0.0),
    WorldFactKind.STALE_FACT: (UncertaintyLevel.STALE, 0.3),
}

_CONFLICTING_CONFIDENCE = 0.3
_MAX_FACTS_PER_OBSERVATION = 16


def _fact_uncertainty(key: str, fact: WorldFact) -> FactUncertainty:
    level, confidence = _LEVELS.get(fact.kind, (UncertaintyLevel.UNCERTAIN, 0.4))
    return FactUncertainty(key=key, level=level, confidence=confidence)


def assess_beliefs(state: BrainState) -> BeliefAssessment:
    """Deterministic uncertainty assessment of the current world model."""
    fact_items = sorted(state.world.facts.items())[:MAX_BELIEF_FACTS]
    uncertainties = [_fact_uncertainty(key, fact) for key, fact in fact_items]

    conflicting = 0
    unverified = 0
    evidence_confidences: list[float] = []
    for hypothesis in state.hypotheses:
        if hypothesis.conflicting_evidence and hypothesis.supporting_evidence:
            conflicting += 1
            evidence_confidences.append(_CONFLICTING_CONFIDENCE)
        else:
            unverified += 1
            evidence_confidences.append(hypothesis.confidence)

    fact_confidences = [u.confidence for u in uncertainties]
    prediction_confidences = [p.confidence for p in state.predictions]
    pool = fact_confidences + evidence_confidences + prediction_confidences
    overall = round(1.0 - (sum(pool) / len(pool)) if pool else 1.0, 4)

    return BeliefAssessment(
        facts=uncertainties,
        overall_uncertainty=overall,
        unverified_hypotheses=unverified,
        conflicting_hypotheses=conflicting,
        verified_facts=sum(1 for u in uncertainties if u.level is UncertaintyLevel.VERIFIED),
        unknown_keys=[key for key, fact in fact_items if fact.kind is WorldFactKind.UNKNOWN],
    )


def apply_observation(state: BrainState, observation: Observation) -> BrainState:
    """Attributable belief update from REAL observed data (Sprint 6).

    Deterministic: each bounded entry of ``observation.data`` becomes a
    FACT-kind world fact (observed, NOT verified); UNKNOWN facts covered
    by the observation become FACT. The world version bumps; the state is
    a NEW frozen instance. Entries beyond the bound are ignored (never
    unbounded)."""
    entries = list(observation.data.items())[:_MAX_FACTS_PER_OBSERVATION]
    if not entries:
        return state
    facts = dict(state.world.facts)
    for key, value in entries:
        key = str(key)[:128]
        if not key.strip():
            continue
        facts[key] = WorldFact(key=key, value=value, kind=WorldFactKind.FACT)
    return state.model_copy(
        update={
            "world": WorldState(
                version=state.world.version + 1, facts=facts
            ),
            "state_version": state.state_version + 1,
        }
    )
