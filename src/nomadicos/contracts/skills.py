"""Skill / capability contracts (Sprint 8).

A skill DESCRIBES what capability it provides — it never executes and
never carries code. Every skill invocation travels the EXISTING runtime
boundary only:

    SkillInvocation → ActionProposal → ProposalValidator →
    AuthorizationService → Executor → ExecutionResult → Verifier

Closed schemas: authority-shaped fields are structurally impossible
(extra="forbid" + invocation-time authority-field rejection reusing the
Action IR parser). Secrets are rejected at definition and invocation
time. All fields bounded; no arbitrary blobs, no callables.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.action import MODEL_AUTHORITY_FIELDS
from nomadicos.contracts.core import Contract
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.ids import new_id

SKILL_ID_CHARS = 64
SKILL_NAME_CHARS = 100
SKILL_DESC_CHARS = 500
MAX_SKILL_PARAMS = 16
MAX_SKILL_PREREQS = 8
MAX_SKILL_ROLES = 8
MAX_SKILL_CONSTRAINTS = 8
MAX_CONSTRAINT_CHARS = 200
MAX_INVOCATION_ARGS = 32
MAX_ARG_KEY_CHARS = 64
MAX_SKILL_MATCH_REASONS = 6

_PARAM_TYPES = ("string", "integer", "number", "boolean", "string_list")
_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")
#: same hygiene as the memory write boundary: secrets never persist here
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
    """Deterministic secret-shape detection for skill text fields."""
    if not isinstance(text, str) or not text:
        return False
    return bool(_SECRET_RE.search(text) or any(p.search(text) for p in _SECRET_SHAPES))


class SkillRisk(StrEnum):
    """Conservative risk ladder; retrieval filters by max risk."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    @property
    def rank(self) -> int:
        return {"LOW": 0, "MEDIUM": 1, "HIGH": 2}[self.value]


class SkillParameter(Contract):
    """One bounded input/output field descriptor (data, not code)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(max_length=MAX_ARG_KEY_CHARS)
    type: Literal["string", "integer", "number", "boolean", "string_list"]
    required: bool = True
    description: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _coherent(self) -> SkillParameter:
        if not self.name.strip() or not _SKILL_ID_RE.match(self.name):
            raise ValueError(f"parameter name must match {_SKILL_ID_RE.pattern}")
        if contains_secret(self.description):
            raise ValueError("skill parameter descriptions must not contain secrets")
        return self


class SkillEvidence(Contract):
    """Measured skill metadata (Sprint 8 evaluation). Nothing is invented:
    zero samples means success_rate is None."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    samples: int = 0
    successes: int = 0
    failures: int = 0
    validation_failures: int = 0
    success_rate: float | None = None
    benchmark_version: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> SkillEvidence:
        if self.samples == 0 and self.success_rate is not None:
            raise ValueError("skill success_rate without samples is invented")
        if self.successes + self.failures != self.samples:
            raise ValueError("skill evidence counts must sum to samples")
        if self.samples and self.success_rate is None:
            raise ValueError("measured skill evidence must carry its success_rate")
        return self


class SkillDefinition(Contract):
    """A bounded, data-only skill description. No callables, no code, no
    secrets — skills are KNOWLEDGE about capability composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(max_length=SKILL_ID_CHARS)
    version: int = Field(default=1, ge=1, le=9999)
    name: str = Field(max_length=SKILL_NAME_CHARS)
    description: str = Field(max_length=SKILL_DESC_CHARS)
    #: the single existing capability this skill provides: "tool.operation"
    capability: str = Field(max_length=128)
    inputs: list[SkillParameter] = Field(default_factory=list)
    outputs: list[SkillParameter] = Field(default_factory=list)
    #: capability ids ("tool.operation") that must exist/be satisfied first
    prerequisites: list[str] = Field(default_factory=list)
    #: brain roles allowed to request this skill; empty = role-agnostic
    supported_roles: list[str] = Field(default_factory=list)
    risk: SkillRisk = SkillRisk.LOW
    constraints: list[str] = Field(default_factory=list)
    enabled: bool = True
    healthy: bool = True
    evidence: SkillEvidence = Field(default_factory=SkillEvidence)

    @model_validator(mode="after")
    def _coherent(self) -> SkillDefinition:
        if not _SKILL_ID_RE.match(self.skill_id):
            raise ValueError(f"skill_id must match {_SKILL_ID_RE.pattern}")
        if not self.name.strip():
            raise ValueError("skill name must not be empty")
        if not self.description.strip():
            raise ValueError("skill description must not be empty")
        if not _CAPABILITY_RE.match(self.capability):
            raise ValueError(f"capability must be 'tool.operation', got {self.capability!r}")
        if len(self.inputs) > MAX_SKILL_PARAMS or len(self.outputs) > MAX_SKILL_PARAMS:
            raise ValueError(f"skill params exceed {MAX_SKILL_PARAMS}")
        if len(self.prerequisites) > MAX_SKILL_PREREQS:
            raise ValueError(f"skill prerequisites exceed {MAX_SKILL_PREREQS}")
        if len(self.supported_roles) > MAX_SKILL_ROLES:
            raise ValueError(f"skill roles exceed {MAX_SKILL_ROLES}")
        if len(self.constraints) > MAX_SKILL_CONSTRAINTS:
            raise ValueError(f"skill constraints exceed {MAX_SKILL_CONSTRAINTS}")
        for constraint in self.constraints:
            if not constraint.strip() or len(constraint) > MAX_CONSTRAINT_CHARS:
                raise ValueError("skill constraint must be 1..200 chars")
        for param in self.inputs + self.outputs:
            if contains_secret(param.description):
                raise ValueError("skill parameters must not contain secrets")
        for field_text in (self.name, self.description, *self.constraints):
            if contains_secret(field_text):
                raise ValueError("skill definitions must not contain secrets")
        if len({p.name for p in self.inputs}) != len(self.inputs):
            raise ValueError("duplicate input parameter names")
        return self


class SkillQuery(Contract):
    """A bounded capability request (deterministic retrieval input)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(max_length=128)
    role: str = Field(default="", max_length=64)
    max_candidates: int = Field(default=5, ge=1, le=32)
    #: input names the caller can supply (compatibility filter)
    provided_args: list[str] = Field(default_factory=list)
    #: capability ids already satisfied (prerequisite filter)
    satisfied_prerequisites: list[str] = Field(default_factory=list)
    max_risk: SkillRisk = SkillRisk.MEDIUM

    @model_validator(mode="after")
    def _coherent(self) -> SkillQuery:
        if not _CAPABILITY_RE.match(self.capability):
            raise ValueError("query capability must be 'tool.operation'")
        if len(self.provided_args) > MAX_SKILL_PARAMS:
            raise ValueError(f"provided args exceed {MAX_SKILL_PARAMS}")
        if len(self.satisfied_prerequisites) > MAX_SKILL_PREREQS:
            raise ValueError(f"satisfied prerequisites exceed {MAX_SKILL_PREREQS}")
        return self


class SkillMatch(Contract):
    """One retrieval hit with its deterministic score + reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    version: int
    capability: str
    score: float = Field(ge=0.0, le=1.0)
    measured: bool = False
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SkillMatch:
        if len(self.reasons) > MAX_SKILL_MATCH_REASONS:
            raise ValueError(f"skill match reasons exceed {MAX_SKILL_MATCH_REASONS}")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("skill match reasons must not be empty")
        return self


class SkillRetrieval(Contract):
    """Deterministic retrieval outcome: bounded matches + auditable
    exclusions (fail-safe visibility, never silent)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str
    matches: list[SkillMatch] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SkillRetrieval:
        if len(self.matches) > 32 or len(self.excluded) > 32:
            raise ValueError("skill retrieval out of bounds")
        return self


class SkillInvocation(Contract):
    """A bounded skill invocation request — DATA only. It becomes an
    ActionProposal via the adapter; it never executes anything itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(default_factory=lambda: new_id("task"))
    skill_id: str = Field(max_length=SKILL_ID_CHARS)
    version: int = Field(ge=1)
    args: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="", max_length=64)
    task_id: str = ""
    step_id: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> SkillInvocation:
        if not self.skill_id.strip():
            raise ValueError("invocation requires a skill id")
        if len(self.args) > MAX_INVOCATION_ARGS:
            raise ValueError(f"invocation args exceed {MAX_INVOCATION_ARGS}")
        for key, value in self.args.items():
            if not isinstance(key, str) or not key or len(key) > MAX_ARG_KEY_CHARS:
                raise ValueError("invocation arg keys must be short strings")
            if isinstance(value, (dict,)) or value is None:
                raise ValueError("invocation arg values must be scalars or string lists")
            if isinstance(value, list) and not all(isinstance(v, str) for v in value):
                raise ValueError("list invocation args must be string lists")
            if isinstance(value, str) and contains_secret(value):
                raise ValueError("invocation args must not contain secrets")
        lowered = {str(k).lower() for k in self.args}
        clash = lowered & {f.lower() for f in MODEL_AUTHORITY_FIELDS}
        if clash:
            raise ValueError(f"invocation args carry authority-shaped fields: {sorted(clash)}")
        return self


class SkillValidationResult(Contract):
    """Structural validation verdict ("is this invocation well-formed?").

    NOT authorization: authorization stays exclusively with the existing
    AuthorizationService."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    invocation_id: str
    skill_id: str
    reasons: list[str] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SkillValidationResult:
        if not self.ok and not self.reasons:
            raise ValueError("a failed skill validation requires reasons")
        if self.ok and self.reasons:
            raise ValueError("a passed skill validation must not carry failure reasons")
        return self


class SkillResult(Contract):
    """Structured skill outcome mapped from an existing ExecutionResult."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    version: int
    invocation_id: str
    status: ExecutionStatus
    execution_id: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    failure_category: str = ""
    message: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _coherent(self) -> SkillResult:
        if len(self.evidence) > 16:
            raise ValueError("skill result evidence exceeds 16 entries")
        for key, value in self.evidence.items():
            if not isinstance(key, str) or not key or len(key) > MAX_ARG_KEY_CHARS:
                raise ValueError("skill result evidence keys must be short strings")
            if isinstance(value, (dict, list)) and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)
            ):
                raise ValueError("skill result evidence values must be scalars or string lists")
            if isinstance(value, str) and contains_secret(value):
                raise ValueError("skill result evidence must not contain secrets")
        return self


class SkillEvaluationRecord(Contract):
    """Deterministic evaluation summary over recorded outcomes (audit)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    version: int
    cases: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    validation_failures: int = Field(ge=0)
    success_rate: float | None = None
    benchmark_version: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherent(self) -> SkillEvaluationRecord:
        if self.successes + self.failures + self.validation_failures != self.cases:
            raise ValueError("skill evaluation counts must sum to cases")
        if self.cases and self.success_rate is None:
            raise ValueError("measured evaluation must carry its success_rate")
        return self
