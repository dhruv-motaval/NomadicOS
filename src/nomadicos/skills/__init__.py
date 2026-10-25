"""Sprint 8 — Skills / Capability System.

Skill knowledge (declarative, bounded, code-free) + deterministic
retrieval/validation + a narrow adapter into the EXISTING Action IR.
No second execution architecture: every invocation travels
ActionProposal → ProposalValidator → AuthorizationService → Executor →
Verifier.
"""

from nomadicos.contracts.skills import (
    SkillDefinition,
    SkillEvaluationRecord,
    SkillEvidence,
    SkillInvocation,
    SkillMatch,
    SkillParameter,
    SkillQuery,
    SkillResult,
    SkillRetrieval,
    SkillRisk,
    SkillValidationResult,
    contains_secret,
)
from nomadicos.skills.adapter import parse_capability, to_proposal, to_result
from nomadicos.skills.evaluation import evaluate, record_outcome
from nomadicos.skills.registry import SkillRegistry
from nomadicos.skills.retrieval import rank_skills
from nomadicos.skills.validation import validate_invocation

__all__ = [
    "SkillDefinition",
    "SkillEvidence",
    "SkillEvaluationRecord",
    "SkillInvocation",
    "SkillMatch",
    "SkillParameter",
    "SkillQuery",
    "SkillRegistry",
    "SkillResult",
    "SkillRetrieval",
    "SkillRisk",
    "SkillValidationResult",
    "contains_secret",
    "evaluate",
    "parse_capability",
    "rank_skills",
    "record_outcome",
    "to_proposal",
    "to_result",
    "validate_invocation",
]
