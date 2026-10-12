"""Adaptive effort — explicit bounded effort/budget policy (Sprint 6).

The brain decides HOW MUCH computation the next planning step deserves:
additional search, more refinement iterations, or information gathering
first — and when to stop. Deterministic rules over (beliefs,
metacognition, budget, plan state):

    budget exhausted                        → STOP (steps at budget bounds)
    metacognition recommends GATHER_INFO    → GATHER_INFO (observe-first)
    high uncertainty + subgoal to plan      → FULL_EFFORT  (deep search)
    low uncertainty + existing decision     → REDUCED_EFFORT (shallow)
    otherwise                               → FULL_EFFORT (default)

HARD BOUNDS (never configuration, never model output): iterations ≤
``MAX_ITERATIONS_BOUND``, search depth ≤ ``SEARCH_MAX_DEPTH``, and the
plan can only ever ADVISE the existing deterministic loops — the Actor/
Monitor loop budget, monitor gate, authority, and verification are
untouched. Adaptive effort can never become an unbounded loop.
"""

from __future__ import annotations

from nomadicos.brain.metacognition import SEARCH_DEEPER_THRESHOLD, assess_metacognition
from nomadicos.brain.schemas import (
    MAX_ITERATIONS_BOUND,
    SEARCH_MAX_DEPTH,
    BeliefAssessment,
    EffortDecision,
    EffortPlan,
    MetacognitiveAssessment,
    MetacognitiveRecommendation,
)
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import assess_beliefs

#: fixed deterministic thresholds (spec: "low-uncertainty case should
#: terminate earlier; high-uncertainty may justify more, within limits")
REDUCED_UNCERTAINTY = 0.25
FULL_ITERATIONS = 3
REDUCED_ITERATIONS = 1

_REASON_CHARS = 200


def decide_effort(
    state: BrainState,
    beliefs: BeliefAssessment | None = None,
    metacognition: MetacognitiveAssessment | None = None,
) -> EffortPlan:
    """Deterministic bounded effort plan for the next planning step."""
    budget = state.budget
    exhausted = budget.steps_used >= budget.max_steps
    if exhausted:
        return EffortPlan(
            decision=EffortDecision.STOP,
            max_iterations=1,
            search_depth=0,
            reasons=["budget exhausted: no further computation is justified"],
        )

    beliefs = beliefs if beliefs is not None else assess_beliefs(state)
    metacognition = (
        metacognition
        if metacognition is not None
        else assess_metacognition(state, beliefs)
    )

    if metacognition.recommendation is MetacognitiveRecommendation.GATHER_INFO:
        return EffortPlan(
            decision=EffortDecision.GATHER_INFO,
            max_iterations=1,
            search_depth=0,
            reasons=[
                "unknown facts present: gather information before deeper search",
                *metacognition.reasons,
            ],
        )

    if beliefs.overall_uncertainty >= SEARCH_DEEPER_THRESHOLD:
        return EffortPlan(
            decision=EffortDecision.FULL_EFFORT,
            max_iterations=min(FULL_ITERATIONS, MAX_ITERATIONS_BOUND),
            search_depth=SEARCH_MAX_DEPTH,
            reasons=[
                f"high uncertainty {beliefs.overall_uncertainty:.2f}: "
                f"full bounded effort justified",
            ],
        )

    if (
        beliefs.overall_uncertainty < REDUCED_UNCERTAINTY
        and any(d.selected for d in state.planning_decisions)
    ):
        return EffortPlan(
            decision=EffortDecision.REDUCED_EFFORT,
            max_iterations=REDUCED_ITERATIONS,
            search_depth=0,
            reasons=[
                f"low uncertainty {beliefs.overall_uncertainty:.2f} with an existing "
                f"decision: terminate earlier",
            ],
        )

    return EffortPlan(
        decision=EffortDecision.FULL_EFFORT,
        max_iterations=min(FULL_ITERATIONS, MAX_ITERATIONS_BOUND),
        search_depth=SEARCH_MAX_DEPTH,
        reasons=["default: full bounded effort"],
    )
