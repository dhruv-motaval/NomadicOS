"""Security research lane contracts (Sprint 9).

Security research is a SEPARATELY SCOPED operating mode: nothing here is
valid without an explicit ACTIVE engagement, an explicitly registered
target, an explicitly granted capability, and a valid sandbox — and even
then every action still flows the EXISTING authoritative path:

    SecurityAction → ActionProposal → ProposalValidator →
    AuthorizationService → Executor → ExecutionResult → Verifier →
    Evidence → Audit

Closed schemas everywhere; authority-shaped fields are structurally
impossible; secrets are rejected at definition time and scrubbed before
evidence persistence. Evidence is append-only; audit distinguishes
PROPOSED from EXECUTED — a proposal is never an execution.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.action import MODEL_AUTHORITY_FIELDS
from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id

ENGAGEMENT_ID_CHARS = 64
TARGET_ID_CHARS = 64
SANDBOX_ID_CHARS = 64
TARGET_CANONICAL_CHARS = 200
PURPOSE_CHARS = 500
NOTE_CHARS = 200
MAX_SCOPED_TARGETS = 16
MAX_SCOPED_CAPABILITIES = 16
MAX_SECURITY_ARGS = 16
MAX_ARG_KEY_CHARS = 64
MAX_EVIDENCE_CONTENT_CHARS = 2000
MAX_EVIDENCE_REFS = 8
MAX_AUDIT_REASONS = 8
MAX_SANDBOX_LIMIT = 10_000_000  # hard resource-limit ceiling

_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")
#: same hygiene as the memory write boundary (Sprint 5): secrets never persist
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|password|passwd|pwd|token|"
    r"client[_-]?secret|private[_-]?key)\b\s*[:=]"
)
_SECRET_SHAPES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def contains_secret(text: str) -> bool:
    """Deterministic secret-shape detection for security text fields."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_SECRET_RE.search(text) or any(p.search(text) for p in _SECRET_SHAPES))


# ════════════════════════════ engagement scope ════════════════════════════


class EngagementStatus(StrEnum):
    """Explicit engagement lifecycle. Only ACTIVE may authorize research."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    REVOKED = "REVOKED"


class SandboxClass(StrEnum):
    """Increasing isolation strength (rank-ordered, fail-closed checks)."""

    LOCAL = "LOCAL"
    WORKSPACE = "WORKSPACE"
    ISOLATED = "ISOLATED"

    @property
    def rank(self) -> int:
        return {"LOCAL": 0, "WORKSPACE": 1, "ISOLATED": 2}[self.value]


class SandboxPolicy(Contract):
    """Bounded sandbox constraints attached to an engagement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: no network from sandboxes unless explicitly sandboxed
    network: Literal["none", "sandboxed"] = "none"
    #: filesystem reach: nothing, workspace-rooted, or full sandbox
    filesystem: Literal["none", "workspace", "sandbox"] = "workspace"
    #: process policy: no processes or bounded ones (never unrestricted)
    process: Literal["none", "bounded"] = "bounded"
    max_cpu_seconds: int = Field(default=60, ge=1, le=MAX_SANDBOX_LIMIT)
    max_memory_mb: int = Field(default=512, ge=1, le=MAX_SANDBOX_LIMIT)


class Engagement(Contract):
    """Explicit, bounded authorization scope for security research.

    The engagement is the OWNER's authorization artifact: capabilities
    are granted here and nowhere else. It grants NO execution power by
    itself — execution still requires the full existing pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engagement_id: str = Field(max_length=ENGAGEMENT_ID_CHARS)
    owner_reference: str = Field(max_length=128)
    purpose: str = Field(max_length=PURPOSE_CHARS)
    #: explicitly registered target ids in scope (no discovery)
    allowed_targets: list[str] = Field(default_factory=list)
    #: explicitly granted security capability kinds
    allowed_capabilities: list[str] = Field(default_factory=list)
    sandbox_policy: SandboxPolicy = Field(default_factory=SandboxPolicy)
    #: bounded action budget for the whole engagement
    max_actions: int = Field(default=10, ge=1, le=1000)
    status: EngagementStatus = EngagementStatus.DRAFT
    evidence_policy: Literal["APPEND_ONLY"] = "APPEND_ONLY"
    audit_policy: Literal["FULL"] = "FULL"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherent(self) -> Engagement:
        if not _ID_RE.match(self.engagement_id):
            raise ValueError(f"engagement_id must match {_ID_RE.pattern}")
        if not self.owner_reference.strip():
            raise ValueError("engagement requires an owner reference")
        if not self.purpose.strip():
            raise ValueError("engagement requires a purpose")
        if len(self.allowed_targets) > MAX_SCOPED_TARGETS:
            raise ValueError(f"engagement targets exceed {MAX_SCOPED_TARGETS}")
        if len(self.allowed_capabilities) > MAX_SCOPED_CAPABILITIES:
            raise ValueError(f"engagement capabilities exceed {MAX_SCOPED_CAPABILITIES}")
        for target in self.allowed_targets:
            if not target.strip():
                raise ValueError("engagement targets must not be empty")
        for capability in self.allowed_capabilities:
            if capability not in SECURITY_CAPABILITIES:
                raise ValueError(f"unknown security capability {capability!r}")
        if contains_secret(self.purpose) or contains_secret(self.owner_reference):
            raise ValueError("engagement metadata must not contain secrets")
        return self


#: deterministic lifecycle transitions (everything else fails closed)
ENGAGEMENT_TRANSITIONS: dict[EngagementStatus, frozenset[EngagementStatus]] = {
    EngagementStatus.DRAFT: frozenset(
        {EngagementStatus.ACTIVE, EngagementStatus.REVOKED}
    ),
    EngagementStatus.ACTIVE: frozenset(
        {
            EngagementStatus.SUSPENDED,
            EngagementStatus.COMPLETED,
            EngagementStatus.REVOKED,
        }
    ),
    EngagementStatus.SUSPENDED: frozenset(
        {EngagementStatus.ACTIVE, EngagementStatus.REVOKED}
    ),
    EngagementStatus.COMPLETED: frozenset(),
    EngagementStatus.REVOKED: frozenset(),
}


def transition_engagement(
    engagement: Engagement, to_status: EngagementStatus
) -> Engagement:
    """Deterministic lifecycle transition; illegal transitions fail closed."""
    if to_status not in ENGAGEMENT_TRANSITIONS[engagement.status]:
        raise ValueError(
            f"illegal engagement transition {engagement.status.value} -> {to_status.value}"
        )
    return engagement.model_copy(update={"status": to_status})


# ══════════════════════════════ target registry ══════════════════════════════


class TargetType(StrEnum):
    """Closed target taxonomy — authorization inputs, NOT a discovery kit."""

    LOCAL_SANDBOX = "LOCAL_SANDBOX"
    TEST_APPLICATION = "TEST_APPLICATION"
    REGISTERED_ENDPOINT = "REGISTERED_ENDPOINT"
    REPOSITORY = "REPOSITORY"
    CONTAINER = "CONTAINER"
    TEST_DATASET = "TEST_DATASET"


class TargetRecord(Contract):
    """One explicitly registered target. Registration is an authorization
    INPUT — this registry never discovers real-world targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(max_length=TARGET_ID_CHARS)
    type: TargetType
    #: canonical identifier (bounded; e.g. repo path or host-scoped URL)
    canonical_identifier: str = Field(max_length=TARGET_CANONICAL_CHARS)
    #: existing tool.operations this target may serve (bounded)
    allowed_operations: list[str] = Field(default_factory=list)
    environment: Literal["local", "sandbox"] = "local"
    enabled: bool = True
    #: optional engagement binding (empty = any active engagement in scope)
    engagement_id: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> TargetRecord:
        if not _ID_RE.match(self.target_id):
            raise ValueError(f"target_id must match {_ID_RE.pattern}")
        if not self.canonical_identifier.strip():
            raise ValueError("target requires a canonical identifier")
        if len(self.allowed_operations) > MAX_SCOPED_CAPABILITIES:
            raise ValueError(f"target operations exceed {MAX_SCOPED_CAPABILITIES}")
        for operation in self.allowed_operations:
            if not _CAPABILITY_RE.match(operation):
                raise ValueError(f"target operation must be 'tool.operation': {operation!r}")
        if contains_secret(self.canonical_identifier):
            raise ValueError("target identifiers must not contain secrets")
        return self


# ══════════════════════════ security capability taxonomy ══════════════════════════


class SecurityCapabilityKind(StrEnum):
    """Closed, high-level, authorization-oriented capability taxonomy.

    Deliberately EXCLUDES exploit generation, credential theft,
    persistence, evasion, destructive actions, and unrestricted scanning —
    the lane measures and validates; it is not an offense toolkit."""

    RECON = "RECON"
    ANALYSIS = "ANALYSIS"
    CONFIGURATION_REVIEW = "CONFIGURATION_REVIEW"
    CODE_ANALYSIS = "CODE_ANALYSIS"
    DEPENDENCY_ANALYSIS = "DEPENDENCY_ANALYSIS"
    VULNERABILITY_VALIDATION = "VULNERABILITY_VALIDATION"
    EVIDENCE_COLLECTION = "EVIDENCE_COLLECTION"


class SecurityRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class EvidenceType(StrEnum):
    OBSERVATION = "OBSERVATION"
    FINDING = "FINDING"
    ARTIFACT = "ARTIFACT"
    MEASUREMENT = "MEASUREMENT"


class SecurityCapabilitySpec(Contract):
    """One closed taxonomy entry: what the capability requires and yields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SecurityCapabilityKind
    risk: SecurityRisk
    #: engagement must explicitly grant this kind (always true in v1)
    requires_permission: bool = True
    #: minimum sandbox isolation class
    sandbox_class: SandboxClass
    #: max bounded args the action may carry
    max_args: int = Field(default=8, ge=1, le=MAX_SECURITY_ARGS)
    evidence_type: EvidenceType
    description: str = Field(max_length=PURPOSE_CHARS)

    @model_validator(mode="after")
    def _coherent(self) -> SecurityCapabilitySpec:
        if contains_secret(self.description):
            raise ValueError("capability descriptions must not contain secrets")
        return self


#: the closed taxonomy (module constant — brain/model output cannot extend it)
SECURITY_CAPABILITIES: dict[str, SecurityCapabilitySpec] = {
    spec.kind.value: spec
    for spec in (
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.RECON,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.OBSERVATION,
            description="enumerate explicitly registered target facts (no discovery)",
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.ANALYSIS,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.FINDING,
            description="analyze explicitly provided artifacts inside the sandbox",
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.CONFIGURATION_REVIEW,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.FINDING,
            description="review configuration files of in-scope targets",
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.CODE_ANALYSIS,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.FINDING,
            description="static analysis of in-scope repository code",
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.DEPENDENCY_ANALYSIS,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.MEASUREMENT,
            description="analyze declared dependencies of in-scope targets",
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.VULNERABILITY_VALIDATION,
            risk=SecurityRisk.HIGH,
            sandbox_class=SandboxClass.ISOLATED,
            evidence_type=EvidenceType.MEASUREMENT,
            description=(
                "validate one suspected weakness against the in-scope test "
                "target inside an ISOLATED sandbox"
            ),
        ),
        SecurityCapabilitySpec(
            kind=SecurityCapabilityKind.EVIDENCE_COLLECTION,
            risk=SecurityRisk.LOW,
            sandbox_class=SandboxClass.WORKSPACE,
            evidence_type=EvidenceType.ARTIFACT,
            description="collect and bind evidence for audited operations",
        ),
    )
}


# ══════════════════════════════ sandbox ══════════════════════════════


class SandboxRecord(Contract):
    """One instantiated sandbox binding: engagement + target + policy.

    A sandbox is POLICY DATA. It grants no authority; authorization
    remains exclusively with the existing AuthorizationService."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_id: str = Field(max_length=SANDBOX_ID_CHARS)
    engagement_id: str = Field(max_length=ENGAGEMENT_ID_CHARS)
    target_id: str = Field(max_length=TARGET_ID_CHARS)
    sandbox_class: SandboxClass
    policy: SandboxPolicy
    #: capabilities this sandbox instance permits (⊆ engagement grant)
    allowed_capabilities: list[str] = Field(default_factory=list)
    active: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> SandboxRecord:
        if not _ID_RE.match(self.sandbox_id):
            raise ValueError(f"sandbox_id must match {_ID_RE.pattern}")
        if len(self.allowed_capabilities) > MAX_SCOPED_CAPABILITIES:
            raise ValueError(f"sandbox capabilities exceed {MAX_SCOPED_CAPABILITIES}")
        for capability in self.allowed_capabilities:
            if capability not in SECURITY_CAPABILITIES:
                raise ValueError(f"unknown sandbox capability {capability!r}")
        return self


# ════════════════════════════ security action ════════════════════════════


class SecurityAction(Contract):
    """A bounded security-research proposal. DATA only — it must NOT
    contain self-granted authorization, executor references, credentials,
    unrestricted command strings, or bypass flags (validated)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(default_factory=lambda: new_id("task"))
    engagement_id: str = Field(max_length=ENGAGEMENT_ID_CHARS)
    target_id: str = Field(max_length=TARGET_ID_CHARS)
    sandbox_id: str = Field(max_length=SANDBOX_ID_CHARS)
    capability: SecurityCapabilityKind
    args: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: EvidenceType
    note: str = Field(default="", max_length=NOTE_CHARS)

    @model_validator(mode="after")
    def _coherent(self) -> SecurityAction:
        spec = SECURITY_CAPABILITIES[self.capability.value]
        if len(self.args) > spec.max_args:
            raise ValueError(f"security action args exceed {spec.max_args}")
        for key, value in self.args.items():
            if not isinstance(key, str) or not key or len(key) > MAX_ARG_KEY_CHARS:
                raise ValueError("security arg keys must be short strings")
            if isinstance(value, (dict,)) or value is None:
                raise ValueError("security arg values must be scalars or string lists")
            if isinstance(value, list) and not all(isinstance(v, str) for v in value):
                raise ValueError("list security args must be string lists")
            if isinstance(value, str) and contains_secret(value):
                raise ValueError("security args must not contain secrets")
        lowered = {str(k).lower() for k in self.args}
        clash = lowered & {f.lower() for f in MODEL_AUTHORITY_FIELDS}
        if clash:
            raise ValueError(
                f"security args carry authority-shaped fields: {sorted(clash)}"
            )
        if contains_secret(self.note):
            raise ValueError("security action notes must not contain secrets")
        return self


# ══════════════════════════════ evidence ══════════════════════════════


class SecurityEvidence(Contract):
    """Append-only, provenance-preserving security evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    engagement_id: str
    target_id: str
    capability: str
    sequence: int = Field(ge=0)
    source: str = Field(default="", max_length=128)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    classification: EvidenceType
    #: scrubbed content or a bounded reference (never raw secrets)
    content: str = Field(default="", max_length=MAX_EVIDENCE_CONTENT_CHARS)
    provenance: list[str] = Field(default_factory=list)
    invocation_id: str = ""
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherent(self) -> SecurityEvidence:
        bindings = (
            self.evidence_id.strip(),
            self.engagement_id.strip(),
            self.target_id.strip(),
        )
        if not all(bindings):
            raise ValueError("evidence requires engagement/target/capability binding")
        if len(self.provenance) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence provenance exceeds {MAX_EVIDENCE_REFS}")
        for ref in self.provenance:
            if not ref.strip():
                raise ValueError("evidence provenance entries must not be empty")
        if contains_secret(self.content) or any(contains_secret(r) for r in self.provenance):
            raise ValueError("evidence must not contain secrets (scrub before persist)")
        return self


# ══════════════════════════════ audit ══════════════════════════════


class AuditRecord(Contract):
    """One auditable trace entry. PROPOSED is never EXECUTED: executed=True
    requires a real execution result reference (never a proposal)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    sequence: int = Field(ge=0)
    engagement_id: str
    target_id: str
    sandbox_id: str = ""
    capability: str
    invocation_id: str = ""
    proposed: bool = False
    validated: bool | None = None
    authorized: bool | None = None
    executed: bool | None = None
    verified: bool | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    execution_ref: str = ""
    failure: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> AuditRecord:
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"audit evidence refs exceed {MAX_EVIDENCE_REFS}")
        if self.executed and not self.execution_ref:
            raise ValueError(
                "an EXECUTED audit record requires a real execution result "
                "reference — proposals are never executions"
            )
        if len(self.failure) > NOTE_CHARS:
            raise ValueError("audit failure text exceeds bound")
        return self
