"""Brain security seam (Sprint 9) — the NARROW security-research
protocol, mirroring StructuredGenerator / MemoryRetriever /
RoleModelRouter / SkillCatalog.

The brain:
- states a security-research need (``SecurityCapabilityRequest`` — data),
- asks an external adapter which capabilities are available for the
  engagement + target (``SecurityCatalog`` protocol, plain dict answers,
  brain-validated fail-closed),
- produces a bounded ``SecurityResearchPlan`` proposal artifact.

The brain NEVER: imports concrete security tools, OS/subprocess/network
APIs, executes anything, authorizes anything, or bypasses the existing
gates — the plan is DATA for the composition layer, which drives the
engagement/target/capability/sandbox validation pipeline and then the
existing Action IR → validator → authorization → executor path. The
brain is model-agnostic and execution-agnostic here as everywhere.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.contracts.core import Contract
from nomadicos.contracts.security import (
    SECURITY_CAPABILITIES,
    EvidenceType,
)

MAX_PLAN_ARGS = 8
MAX_PLAN_RATIONALE_CHARS = 200
_ENGAGEMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")


class SecurityCapabilityRequest(Contract):
    """A bounded security-research need (data only, no authority)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engagement_id: str = Field(max_length=64)
    target_id: str = Field(max_length=64)
    #: optionally narrow to one capability kind
    capability: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _coherent(self) -> SecurityCapabilityRequest:
        if not _ENGAGEMENT_RE.match(self.engagement_id):
            raise ValueError("security request requires a valid engagement id")
        if not _TARGET_RE.match(self.target_id):
            raise ValueError("security request requires a valid target id")
        if self.capability and self.capability not in SECURITY_CAPABILITIES:
            raise ValueError(f"unknown security capability {self.capability!r}")
        return self


class SecurityOption(Contract):
    """Brain-validated availability answer (data; never authority)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(max_length=64)
    engagement_id: str = Field(max_length=64)
    target_id: str = Field(max_length=64)
    sandbox_id: str = Field(default="", max_length=64)
    risk: str = Field(default="LOW", max_length=16)
    available: bool = True
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SecurityOption:
        if self.capability not in SECURITY_CAPABILITIES:
            raise ValueError(f"unknown security capability {self.capability!r}")
        if len(self.reasons) > 6:
            raise ValueError("security option reasons exceed 6")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("security option reasons must not be empty")
        return self


class SecurityResearchPlan(Contract):
    """The brain's bounded security proposal artifact (DATA only).

    The composition layer turns this into a SecurityAction and drives the
    FULL validation pipeline (engagement → target → capability → sandbox
    → Action IR → authorization → executor → evidence → audit). The plan
    carries NO execution or authorization semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engagement_id: str = Field(max_length=64)
    target_id: str = Field(max_length=64)
    sandbox_id: str = Field(max_length=64)
    capability: str = Field(max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    expected_evidence: str = Field(max_length=32)
    rationale: str = Field(default="", max_length=MAX_PLAN_RATIONALE_CHARS)

    @model_validator(mode="after")
    def _coherent(self) -> SecurityResearchPlan:
        if self.capability not in SECURITY_CAPABILITIES:
            raise ValueError(f"unknown security capability {self.capability!r}")
        if len(self.args) > MAX_PLAN_ARGS:
            raise ValueError(f"plan args exceed {MAX_PLAN_ARGS}")
        if self.expected_evidence not in {t.value for t in EvidenceType}:
            raise ValueError("plan expected_evidence must be an EvidenceType value")
        return self


@runtime_checkable
class SecurityCatalog(Protocol):
    """The ONLY security seam the brain knows (structural protocol —
    satisfied by the security brick's catalog adapter)."""

    def security_capabilities(
        self, request: SecurityCapabilityRequest
    ) -> list[dict[str, Any]]: ...


def parse_security_options(
    data: list[dict[str, Any]],
    request: SecurityCapabilityRequest,
) -> list[SecurityOption]:
    """Validate adapter answers (fail closed): malformed entries,
    unknown capabilities, binding mismatches, and authority-shaped extras
    are rejected — never partially accepted."""
    options: list[SecurityOption] = []
    for entry in data:
        payload = dict(entry)
        payload.setdefault("engagement_id", request.engagement_id)
        payload.setdefault("target_id", request.target_id)
        if payload["engagement_id"] != request.engagement_id or payload[
            "target_id"
        ] != request.target_id:
            raise ModelOutputRejected(
                "security option binding mismatch (no target/engagement substitution)"
            )
        try:
            options.append(SecurityOption.model_validate(payload))
        except ValidationError as exc:
            raise ModelOutputRejected(
                "security option rejected",
                detail=str(exc.errors()[:3]),
            ) from None
    return options


def select_security_option(
    options: list[SecurityOption],
) -> SecurityOption | None:
    """Deterministic selection: lowest-risk available option, ties broken
    by capability name. None when nothing is available (fail safe — the
    brain never substitutes an unapproved capability)."""
    available = [option for option in options if option.available]
    if not available:
        return None
    risk_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    return sorted(
        available,
        key=lambda option: (risk_rank.get(option.risk, 3), option.capability),
    )[0]


def plan_security_research(
    option: SecurityOption,
    sandbox_id: str,
    args: dict[str, Any],
    *,
    rationale: str = "",
) -> SecurityResearchPlan:
    """Bounded proposal artifact for the selected capability."""
    spec = SECURITY_CAPABILITIES[option.capability]
    return SecurityResearchPlan(
        engagement_id=option.engagement_id,
        target_id=option.target_id,
        sandbox_id=sandbox_id,
        capability=option.capability,
        args=dict(args),
        expected_evidence=spec.evidence_type.value,
        rationale=rationale[:MAX_PLAN_RATIONALE_CHARS],
    )


__all__ = [
    "SecurityCapabilityRequest",
    "SecurityCatalog",
    "SecurityOption",
    "SecurityResearchPlan",
    "parse_security_options",
    "plan_security_research",
    "select_security_option",
]
