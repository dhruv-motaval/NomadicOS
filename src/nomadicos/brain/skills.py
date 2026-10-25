"""Brain skill seam (Sprint 8) — the NARROW capability/skill boundary.

Exactly like the model seam (``StructuredGenerator``), the memory seam
(``MemoryRetriever``), and the routing seam (``RoleModelRouter``), the
brain speaks about skills ONLY through a protocol:

    SkillCatalog.find_skills(request) -> list[dict]   (adapter answers)

The brain:
- states a capability need (``SkillRequest`` — data),
- receives bounded candidate skills (brain-validated ``SkillOption``),
- selects deterministically (top option = highest score, stable order),
- produces a bounded ``SkillInvocationPlan`` proposal artifact.

The brain NEVER: imports concrete tool implementations, executes,
authorizes, or bypasses Action IR/validator/authorization/executor/
verification — the plan is DATA for the composition layer, which turns
it into an ``ActionProposal`` via the skills adapter. Provider- and
tool-agnostic: everything here is opaque ids and scores.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.contracts.core import Contract

MAX_SKILL_OPTIONS = 8
MAX_PLAN_RATIONALE_CHARS = 200
_SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_CAPABILITY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*$")


class SkillRequest(Contract):
    """A bounded capability need from the brain (data only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: str = Field(max_length=128)
    role: str = Field(default="", max_length=64)
    max_candidates: int = Field(default=5, ge=1, le=32)
    provided_args: list[str] = Field(default_factory=list)
    satisfied_prerequisites: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SkillRequest:
        if not _CAPABILITY_RE.match(self.capability):
            raise ValueError("skill request capability must be 'tool.operation'")
        if len(self.provided_args) > 16:
            raise ValueError("provided args exceed 16")
        return self


class SkillOption(Contract):
    """Brain-validated retrieval hit (ids + scores; never code)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    version: int = Field(ge=1)
    capability: str = Field(max_length=128)
    score: float = Field(ge=0.0, le=1.0)
    measured: bool = False
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SkillOption:
        if not _SKILL_ID_RE.match(self.skill_id):
            raise ValueError(f"invalid skill id {self.skill_id!r}")
        if not _CAPABILITY_RE.match(self.capability):
            raise ValueError("skill option capability must be 'tool.operation'")
        if len(self.reasons) > 6:
            raise ValueError("skill option reasons exceed 6")
        return self


class SkillInvocationPlan(Contract):
    """The brain's bounded skill proposal artifact (DATA only).

    The composition layer translates this into a SkillInvocation →
    ActionProposal; the existing runtime then validates, authorizes,
    executes, and verifies. The plan carries NO authority semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    version: int = Field(ge=1)
    capability: str = Field(max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="", max_length=64)
    rationale: str = Field(default="", max_length=MAX_PLAN_RATIONALE_CHARS)

    @model_validator(mode="after")
    def _coherent(self) -> SkillInvocationPlan:
        if not _SKILL_ID_RE.match(self.skill_id):
            raise ValueError(f"invalid skill id {self.skill_id!r}")
        if not _CAPABILITY_RE.match(self.capability):
            raise ValueError("plan capability must be 'tool.operation'")
        if len(self.args) > 32:
            raise ValueError("plan args exceed 32")
        return self


@runtime_checkable
class SkillCatalog(Protocol):
    """The ONLY skill seam the brain knows (structural protocol —
    satisfied by the skills brick's deterministic retriever)."""

    def find_skills(self, request: SkillRequest) -> list[dict[str, Any]]: ...


def parse_skill_options(
    data: list[dict[str, Any]],
    request: SkillRequest,
) -> list[SkillOption]:
    """Validate adapter answers into SkillOptions (fail closed).

    Malformed entries, authority-shaped fields, out-of-range scores, and
    capability mismatches are REJECTED — never partially accepted. The
    deterministic selection order (-score, skill_id, -version) is
    enforced here: an out-of-order answer is rejected."""
    options: list[SkillOption] = []
    for entry in data:
        payload = dict(entry)
        payload.setdefault("capability", request.capability)
        if payload["capability"] != request.capability:
            raise ModelOutputRejected(
                "skill option capability mismatch",
                detail=f"expected {request.capability}",
            )
        try:
            options.append(SkillOption.model_validate(payload))
        except ValidationError as exc:
            raise ModelOutputRejected(
                "skill option rejected",
                detail=str(exc.errors()[:3]),
            ) from None
    if options != sorted(options, key=lambda o: (-o.score, o.skill_id, -o.version)):
        raise ModelOutputRejected(
            "skill options are not in deterministic selection order",
        )
    if len(options) > request.max_candidates:
        raise ModelOutputRejected(
            f"skill options exceed the requested bound {request.max_candidates}"
        )
    return options


def select_skill(options: list[SkillOption]) -> SkillOption | None:
    """Deterministic selection: the top option, or None when nothing
    qualified (fail-safe: the brain then knows it must act without a
    skill — never silently substituted with another capability)."""
    return options[0] if options else None


def plan_invocation(
    option: SkillOption,
    args: dict[str, Any],
    *,
    requested_by: str = "",
    rationale: str = "",
) -> SkillInvocationPlan:
    """Bounded proposal artifact for the selected skill."""
    return SkillInvocationPlan(
        skill_id=option.skill_id,
        version=option.version,
        capability=option.capability,
        args=dict(args),
        requested_by=requested_by,
        rationale=rationale[:MAX_PLAN_RATIONALE_CHARS],
    )


__all__ = [
    "MAX_SKILL_OPTIONS",
    "SkillCatalog",
    "SkillInvocationPlan",
    "SkillOption",
    "SkillRequest",
    "parse_skill_options",
    "plan_invocation",
    "select_skill",
]
