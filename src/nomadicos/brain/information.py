"""Information gathering — bounded opportunities, no parallel execution
system (Sprint 6).

``identify_information_needs`` derives deterministic, priority-ordered
requests from the belief assessment: UNKNOWN fact keys first, then
low-evidence hypotheses. At most ``MAX_INFO_REQUESTS`` (module constant)
and only while budget remains — information gathering is bounded by the
existing BudgetState, never a free pass.

``to_observe_candidate`` converts a request into a NORMAL
``ActionCandidate(kind=OBSERVE)`` — it travels the existing pipeline
exactly like any other proposal: monitor gate → Action IR → validator →
authority → executor. It NEVER executes directly, never bypasses
authority, and carries no authority-shaped fields (closed schema).
"""

from __future__ import annotations

from nomadicos.brain.schemas import (
    MAX_INFO_REQUESTS,
    ActionCandidate,
    BeliefAssessment,
    CandidateKind,
    InformationRequest,
    _derived_id,
)
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import assess_beliefs

_UNKNOWN_PRIORITY = 1.0
_HYP_PRIORITY = 0.6

_RATIONALE_CHARS = 200


def identify_information_needs(
    state: BrainState,
    beliefs: BeliefAssessment | None = None,
) -> list[InformationRequest]:
    """Deterministic bounded information-gathering requests.

    Priority order: UNKNOWN facts (uncertainty 1.0), then hypotheses
    without supporting evidence (0.6); ties break by target id. Empty
    when the budget is exhausted — gathering respects the budget."""
    budget = state.budget
    if budget.steps_used >= budget.max_steps:
        return []
    beliefs = beliefs if beliefs is not None else assess_beliefs(state)
    requests: list[InformationRequest] = []
    for key in sorted(beliefs.unknown_keys)[:MAX_INFO_REQUESTS]:
        requests.append(
            InformationRequest(
                id=_derived_id("info", "unknown", key),
                target=key,
                rationale=f"fact {key!r} is UNKNOWN; observe before acting"[:_RATIONALE_CHARS],
                priority=_UNKNOWN_PRIORITY,
            )
        )
    if len(requests) >= MAX_INFO_REQUESTS:
        return requests
    for hypothesis in state.hypotheses:
        if hypothesis.supporting_evidence:
            continue
        requests.append(
            InformationRequest(
                id=_derived_id("info", "hypothesis", hypothesis.id),
                target=hypothesis.id,
                rationale=(f"hypothesis {hypothesis.id!r} has no supporting evidence; observe")[
                    :_RATIONALE_CHARS
                ],
                priority=_HYP_PRIORITY,
            )
        )
        if len(requests) >= MAX_INFO_REQUESTS:
            break
    return sorted(requests, key=lambda r: (-r.priority, r.target))


def to_observe_candidate(
    request: InformationRequest,
    *,
    subgoal_id: str | None = None,
    rationale_suffix: str = "",
) -> ActionCandidate:
    """Bridge a request into the EXISTING candidate pipeline.

    The result is a plain OBSERVE candidate: it must pass the same
    monitor gate, Action-IR validation, authorization, and executor
    path as every other proposal. It carries no execution semantics of
    its own."""
    rationale = f"information gathering: {request.rationale}{rationale_suffix}"
    return ActionCandidate(
        id=_derived_id("cand", request.id, "observe"),
        subgoal_id=subgoal_id,
        kind=CandidateKind.OBSERVE,
        expected_effect=(f"reduce uncertainty about {request.target!r} (request {request.id})"),
        rationale=rationale[:2000],
    )


__all__ = [
    "MAX_INFO_REQUESTS",
    "identify_information_needs",
    "to_observe_candidate",
]
