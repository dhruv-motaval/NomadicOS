"""Goal verification brick (SPEC §27-29, §8.1-8.37; §53 Phase 8).

The single authoritative answer to: "did the owner's goal become true?"

- read-only evidence evaluation (filesystem + captured execution evidence)
- typed completion predicates; unknown/unverifiable NEVER passes
- ALL / ANY / NOT bounded composition
- four verdicts: PASS, NOT_PASS, NOT_VERIFIED, BLOCKED
- verifier identity + evidence provenance on every result

It consumes model claims, planner state, and executor results strictly as
data; nothing here grants authority or executes on model input (SPEC §8.33).
"""

from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.goal import PredicateGoalVerifier
from nomadicos.verification.predicates import MAX_DEPTH, evaluate_goal_predicate
from nomadicos.verification.step import PredicateStepVerifier

__all__ = [
    "MAX_DEPTH",
    "EvidenceContext",
    "PredicateGoalVerifier",
    "PredicateStepVerifier",
    "evaluate_goal_predicate",
]
