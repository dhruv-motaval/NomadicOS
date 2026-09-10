"""Self-Improvement Engine (BP §29-31, §65-67, §101, §147, §216, §243; Phase 13).

Improvement is NOT weight modification (answers Section I): candidates are
policy/configuration proposals — sandboxed, benchmarked, compared, versioned,
monitored, rolled back. Evidence-based promotion only (BP §67, §216, §365).
"""

from nomadicos.learning.engine import LearningEngine
from nomadicos.learning.store import (
    CandidateKind,
    CandidateState,
    ImprovementCandidate,
    ImprovementStore,
    PromotionDecision,
)

__all__ = [
    "CandidateKind",
    "CandidateState",
    "ImprovementCandidate",
    "ImprovementStore",
    "LearningEngine",
    "PromotionDecision",
]
