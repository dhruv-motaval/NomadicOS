"""Security research lane (Sprint 9).

A strictly bounded, separately scoped security-research capability:
explicit engagements, explicitly registered targets, explicit capability
grants, sandbox policy, append-only scrubbed evidence, full audit — with
the EXISTING Action IR / validator / authorization / executor path
remaining the ONLY execution authority.
"""

from nomadicos.contracts.security import (
    ENGAGEMENT_TRANSITIONS,
    SECURITY_CAPABILITIES,
    AuditRecord,
    Engagement,
    EngagementStatus,
    EvidenceType,
    SandboxClass,
    SandboxPolicy,
    SandboxRecord,
    SecurityAction,
    SecurityCapabilityKind,
    SecurityCapabilitySpec,
    SecurityEvidence,
    SecurityRisk,
    TargetRecord,
    TargetType,
    contains_secret,
    transition_engagement,
)
from nomadicos.security.audit import AuditTrail
from nomadicos.security.benchmarks import SecurityBenchmarkSuite, run_security_benchmarks
from nomadicos.security.engagement import validate_engagement
from nomadicos.security.evidence import EvidenceStore
from nomadicos.security.sandbox import SandboxManager
from nomadicos.security.targets import TargetRegistry

__all__ = [
    "ENGAGEMENT_TRANSITIONS",
    "SECURITY_CAPABILITIES",
    "AuditRecord",
    "AuditTrail",
    "Engagement",
    "EngagementStatus",
    "EvidenceStore",
    "EvidenceType",
    "SandboxClass",
    "SandboxManager",
    "SandboxPolicy",
    "SandboxRecord",
    "SecurityAction",
    "SecurityBenchmarkSuite",
    "SecurityCapabilityKind",
    "SecurityCapabilitySpec",
    "SecurityEvidence",
    "SecurityRisk",
    "TargetRecord",
    "TargetRegistry",
    "TargetType",
    "contains_secret",
    "run_security_benchmarks",
    "transition_engagement",
    "validate_engagement",
]
