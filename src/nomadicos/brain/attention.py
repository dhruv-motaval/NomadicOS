"""Attention — bounded, deterministic context selection (Sprint 6, spec §9).

Conceptually: 10,000 observations → salience → context selection → a
focused BrainState view. HARD LIMITS: at most ``MAX_ATTENTION_ITEMS``
items (module constant — model output cannot change it); no context
expansion beyond the bound, ever.

Deterministic prioritization over what the brain already has (spec §9
factors, fixed weights, no wall clock — recency is list position, last
entry = most recent):

- goal relevance: distinct-token overlap between the item text and the
  goal objective (+ active subgoal description)
- uncertainty: UNKNOWN/HYPOTHESIS/PREDICTION/STALE facts and low-
  confidence hypotheses rank above settled ones
- recency: later list position wins (deterministic without timestamps)
- dependency: the active subgoal and its candidates rank first
- evidence: memory items carry their Sprint 5 retrieval score as
  relevance (brain → memory stays behind the Sprint 5 protocol seam)

Score = round(clamped weighted sum, 4); ordering (-score, ref) is
enforced by ``AttentionView``. DATA only: attention is a context
selection — it never authorizes, executes, or verifies anything.
"""

from __future__ import annotations

import re

from nomadicos.brain.schemas import (
    MAX_ATTENTION_ITEMS,
    AttentionItem,
    AttentionSource,
    AttentionView,
    WorldFactKind,
)
from nomadicos.brain.state import BrainState

_TOKEN_RE = re.compile(r"\w+")

#: fixed salience weights (deterministic constants, not configuration):
#: goal relevance first (spec §9), then uncertainty, dependency impact,
#: recency — a stale on-target item outranks a fresh off-target one
WEIGHT_GOAL_RELEVANCE = 0.5
WEIGHT_UNCERTAINTY = 0.25
WEIGHT_DEPENDENCY = 0.2
WEIGHT_RECENCY = 0.1

#: uncertainty contribution per world-fact epistemic kind
_FACT_UNCERTAINTY: dict[WorldFactKind, float] = {
    WorldFactKind.VERIFIED_FACT: 0.0,
    WorldFactKind.FACT: 0.2,
    WorldFactKind.HYPOTHESIS: 0.7,
    WorldFactKind.PREDICTION: 0.8,
    WorldFactKind.STALE_FACT: 0.9,
    WorldFactKind.UNKNOWN: 1.0,
}

_SUMMARY_CHARS = 200


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2)


def _relevance(text: str, focus_tokens: frozenset[str]) -> float:
    """Distinct-token coverage of the focus (goal + subgoal) in [0, 1]."""
    if not focus_tokens:
        return 0.0
    return len(focus_tokens & _tokens(text)) / len(focus_tokens)


def _recency(index: int, total: int) -> float:
    """Position-based recency in [0, 1]: last item = 1.0, first = 0.0."""
    if total <= 1:
        return 1.0
    return index / (total - 1)


def _item(
    ref: str,
    source: AttentionSource,
    summary: str,
    *,
    relevance: float,
    uncertainty: float,
    recency: float,
    dependency: float,
    reason_note: str,
) -> AttentionItem:
    score = min(
        1.0,
        round(
            relevance * WEIGHT_GOAL_RELEVANCE
            + uncertainty * WEIGHT_UNCERTAINTY
            + recency * WEIGHT_RECENCY
            + dependency * WEIGHT_DEPENDENCY,
            4,
        ),
    )
    return AttentionItem(
        ref=ref,
        source=source,
        summary=summary[:_SUMMARY_CHARS],
        score=score,
        reasons=[reason_note, f"relevance={relevance:.2f}", f"uncertainty={uncertainty:.2f}"],
    )


def compute_attention(state: BrainState) -> AttentionView:
    """Deterministic bounded salience over the state's cognitive items."""
    focus_parts = [state.goal.objective]
    if state.active_subgoal is not None:
        focus_parts.append(state.active_subgoal.description)
    focus_tokens = _tokens(" ".join(filter(None, focus_parts)))
    items: list[AttentionItem] = []

    if state.active_subgoal is not None:
        items.append(
            _item(
                state.active_subgoal.id,
                AttentionSource.SUBGOAL,
                state.active_subgoal.description,
                relevance=1.0,
                uncertainty=0.0,
                recency=1.0,
                dependency=1.0,
                reason_note="active subgoal (dependency)",
            )
        )

    observations = state.observations
    for index, observation in enumerate(observations):
        items.append(
            _item(
                observation.id,
                AttentionSource.OBSERVATION,
                observation.summary,
                relevance=_relevance(observation.summary, focus_tokens),
                uncertainty=0.1,
                recency=_recency(index, len(observations)),
                dependency=0.0,
                reason_note="observed data",
            )
        )

    hypotheses = state.hypotheses
    for index, hypothesis in enumerate(hypotheses):
        items.append(
            _item(
                hypothesis.id,
                AttentionSource.HYPOTHESIS,
                hypothesis.statement,
                relevance=_relevance(hypothesis.statement, focus_tokens),
                uncertainty=round(1.0 - hypothesis.confidence, 4),
                recency=_recency(index, len(hypotheses)),
                dependency=0.0,
                reason_note="hypothesis",
            )
        )

    fact_items = list(state.world.facts.items())
    for index, (key, fact) in enumerate(fact_items):
        items.append(
            _item(
                key,
                AttentionSource.WORLD_FACT,
                f"{key}={fact.value} ({fact.kind.value})",
                relevance=_relevance(key, focus_tokens),
                uncertainty=_FACT_UNCERTAINTY.get(fact.kind, 0.5),
                recency=_recency(index, len(fact_items)),
                dependency=0.0,
                reason_note=f"world fact ({fact.kind.value})",
            )
        )

    for ref in state.relevant_memories:
        items.append(
            _item(
                ref.memory_id,
                AttentionSource.MEMORY,
                f"retrieved memory (score {ref.score:.2f})",
                relevance=ref.score,
                uncertainty=0.0,
                recency=0.0,
                dependency=0.0,
                reason_note="retrieved memory (Sprint 5 score)",
            )
        )

    candidates = state.action_candidates
    for index, candidate in enumerate(candidates):
        dependency = (
            1.0
            if (
                state.active_subgoal is not None and candidate.subgoal_id == state.active_subgoal.id
            )
            else 0.0
        )
        items.append(
            _item(
                candidate.id,
                AttentionSource.ACTION_CANDIDATE,
                candidate.tool or candidate.kind.value,
                relevance=_relevance(f"{candidate.tool} {candidate.rationale}", focus_tokens),
                uncertainty=0.5,  # proposals are undecided by definition
                recency=_recency(index, len(candidates)),
                dependency=dependency,
                reason_note="pending candidate",
            )
        )

    ordered = sorted(items, key=lambda i: (-i.score, i.ref))[:MAX_ATTENTION_ITEMS]
    return AttentionView(items=ordered)
