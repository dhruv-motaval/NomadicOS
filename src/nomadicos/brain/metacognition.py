"""Metacognition — bounded advisory self-assessment (Sprint 6).

Answers, deterministically and from state data only:

- What do we currently believe?        → ``believed`` (top facts/hypotheses)
- What evidence supports it?           → ``evidence_refs`` (hypothesis +
                                         retrieved-memory references)
- What is uncertain?                   → ``uncertainties``
- What is missing?                     → ``missing`` (UNKNOWN facts,
                                         candidates without predictions)
- Should we continue reasoning?        → ``continue_reasoning`` +
                                         ``recommendation``

ADVISORY ONLY: the assessment is DATA. It cannot authorize execution,
cannot mint artifacts, cannot answer owner conflicts, and cannot mark
anything as an outcome — existing deterministic controls (budget,
monitor gate, authority, verification) stay outside and unweakened.

Recommendation rules (deterministic, fixed thresholds):

1. budget exhausted                                  → STOP
2. unknown facts exist and budget remains            → GATHER_INFO
3. overall uncertainty ≥ 0.5 and candidates exist    → SEARCH_DEEPER
4. otherwise                                         → CONTINUE
"""

from __future__ import annotations

from nomadicos.brain.schemas import (
    MAX_METACOGNITION_ITEMS,
    MAX_PROVENANCE_REFS,
    BeliefAssessment,
    MetacognitiveAssessment,
    MetacognitiveRecommendation,
    UncertaintyLevel,
)
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import assess_beliefs

#: overall uncertainty at/above which deeper search is recommended
SEARCH_DEEPER_THRESHOLD = 0.5

_BELIEVED_CHARS = 120
_REASON_CHARS = 200
_MAX_BELIEVED = 6


def assess_metacognition(
    state: BrainState,
    beliefs: BeliefAssessment | None = None,
) -> MetacognitiveAssessment:
    """Deterministic advisory metacognitive assessment."""
    beliefs = beliefs if beliefs is not None else assess_beliefs(state)
    budget = state.budget
    exhausted = budget.steps_used >= budget.max_steps

    believed: list[str] = []
    for uncertainty in sorted(beliefs.facts, key=lambda u: (-u.confidence, u.key))[:_MAX_BELIEVED]:
        fact = state.world.facts.get(uncertainty.key)
        value = "" if fact is None else str(fact.value)
        believed.append(
            f"{uncertainty.key}={value[:60]} ({uncertainty.level.value})"[:_BELIEVED_CHARS]
        )
    for hypothesis in state.hypotheses[: 2 if len(believed) < _MAX_BELIEVED else 0]:
        believed.append(
            f"hyp {hypothesis.id}: {hypothesis.statement[:60]} ({hypothesis.confidence:.2f})"[
                :_BELIEVED_CHARS
            ]
        )

    evidence_refs: list[str] = []
    for hypothesis in state.hypotheses:
        evidence_refs.extend(hypothesis.supporting_evidence[:2])
    evidence_refs.extend(ref.memory_id for ref in state.relevant_memories)
    evidence_refs = list(dict.fromkeys(evidence_refs))[:MAX_PROVENANCE_REFS]

    uncertainties: list[str] = []
    for uncertainty in beliefs.facts:
        if uncertainty.level is not UncertaintyLevel.CONFIDENT and (
            uncertainty.level is not UncertaintyLevel.VERIFIED
        ):
            uncertainties.append(f"{uncertainty.key}: {uncertainty.level.value}")
    for hypothesis in state.hypotheses:
        if hypothesis.confidence < 0.5:
            uncertainties.append(f"hyp {hypothesis.id}: low confidence")
    uncertainties = uncertainties[:MAX_METACOGNITION_ITEMS]

    missing: list[str] = list(beliefs.unknown_keys)
    predicted_candidates = {p.candidate_id for p in state.predictions}
    for candidate in state.action_candidates:
        if candidate.id not in predicted_candidates:
            missing.append(f"candidate {candidate.id}: no prediction")
    missing = missing[:MAX_METACOGNITION_ITEMS]

    if exhausted:
        recommendation = MetacognitiveRecommendation.STOP
        continue_reasoning = False
        reasons = ["budget exhausted: steps_used reached max_steps"]
    elif beliefs.unknown_keys:
        recommendation = MetacognitiveRecommendation.GATHER_INFO
        continue_reasoning = True
        reasons = [f"{len(beliefs.unknown_keys)} unknown fact(s); gathering beats guessing"]
    elif beliefs.overall_uncertainty >= SEARCH_DEEPER_THRESHOLD and state.action_candidates:
        recommendation = MetacognitiveRecommendation.SEARCH_DEEPER
        continue_reasoning = True
        reasons = [f"overall uncertainty {beliefs.overall_uncertainty:.2f} >= 0.5"]
    else:
        recommendation = MetacognitiveRecommendation.CONTINUE
        continue_reasoning = not exhausted
        reasons = [f"overall uncertainty {beliefs.overall_uncertainty:.2f} below threshold"]

    return MetacognitiveAssessment(
        believed=believed,
        evidence_refs=evidence_refs,
        uncertainties=uncertainties,
        missing=missing,
        continue_reasoning=continue_reasoning,
        recommendation=recommendation,
        reasons=[reason[:_REASON_CHARS] for reason in reasons],
    )
