"""Model independence seam (Sprint 7, spec §15/§56.11-12).

The brain decides WHAT a cognitive role requires — never WHICH provider
serves it. Exactly like the model seam (``StructuredGenerator``), model
selection arrives behind a narrow protocol; the brain imports no
registry, no router, no engine, no provider. BrainState carries model
DATA (ids/scores) only — never live clients, never credentials.

    RoleModelProfile   what the brain needs for a role (deterministic)
    RoleModelRouter    protocol: external adapter answers the selection
    RoleModelSelection brain-validated selection result (fail closed)

Fail-closed rule: adapter output that deviates from the closed schema,
carries authority-shaped fields, or selects nothing is rejected — the
brain keeps its current generator rather than trusting an unqualified
selection. No provider name is ever interpreted here: ``model_id`` and
``engine`` are opaque strings to the brain.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.contracts.core import Contract

MAX_SELECTION_REASONS = 8
MAX_REJECTED = 8
MODEL_ID_CHARS = 200


class RoleModelProfile(Contract):
    """What one cognitive role requires of a model (Sprint 7).

    Deterministic brain-side requirements — the ONLY thing the brain
    says about models. Capability tags reuse the existing cross-brick
    vocabulary; the router maps them to measured evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: CognitiveRole
    #: required measured capabilities (e.g. "text", "reasoning", "tool_use")
    required_capabilities: list[str] = Field(default_factory=list)
    #: minimum usable context window in tokens (bounded, role-derived)
    min_context_tokens: int = Field(default=4096, ge=1, le=1_000_000)
    #: latency intent from Sprint 6 effort policy ("FAST"/"BALANCED"/"QUALITY")
    latency_mode: str = Field(default="BALANCED", max_length=16)
    #: structured-output obligation: every brain role parses closed JSON
    needs_structured_output: bool = True

    @model_validator(mode="after")
    def _coherent(self) -> RoleModelProfile:
        if len(self.required_capabilities) != len(set(self.required_capabilities)):
            raise ValueError("duplicate capability requirements in role profile")
        for capability in self.required_capabilities:
            if not capability.strip():
                raise ValueError("capability requirements must not be empty")
        return self


class RoleModelSelection(Contract):
    """Brain-validated model selection for one role. DATA only: ids and
    scores — never a client, never authority, never a secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: CognitiveRole
    #: opaque model identity (provider-agnostic string; "" = none available)
    model_id: str = Field(max_length=MODEL_ID_CHARS)
    #: opaque engine identity ("" = none available)
    engine: str = Field(default="", max_length=MODEL_ID_CHARS)
    score: float = Field(ge=0.0, le=1.0)
    available: bool = True
    #: deterministic why-explanation (role, capability scores, constraints)
    reasons: list[str] = Field(default_factory=list)
    #: deterministically rejected candidates with reasons (auditability)
    rejected: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> RoleModelSelection:
        if len(self.reasons) > MAX_SELECTION_REASONS:
            raise ValueError(f"selection reasons exceed {MAX_SELECTION_REASONS}")
        if len(self.rejected) > MAX_REJECTED:
            raise ValueError(f"rejected list exceeds {MAX_REJECTED}")
        if not self.available and self.model_id:
            raise ValueError("an unavailable selection must not carry a model id")
        for entry in [*self.reasons, *self.rejected]:
            if not entry.strip():
                raise ValueError("selection metadata entries must not be empty")
        return self


@runtime_checkable
class RoleModelRouter(Protocol):
    """The ONLY model-selection seam the brain knows (Sprint 7).

    Implementations live OUTSIDE the brain (router/registry bricks):
    they own registries, benchmarks, health, canaries. The adapter
    returns plain serializable data; the brain validates it into
    ``RoleModelSelection`` itself — malformed answers fail closed."""

    def select_for_role(self, profile: RoleModelProfile) -> dict[str, Any]: ...


def role_profile(
    role: CognitiveRole,
    *,
    latency_mode: str = "BALANCED",
) -> RoleModelProfile:
    """Deterministic capability requirements per cognitive role (Sprint 7).

    The brain's ONLY model knowledge: every role parses closed-schema
    JSON (structured output) and needs text; reasoning-heavy roles
    require the reasoning capability; the actor needs tool reasoning.
    No provider names — ever."""
    requirements: dict[CognitiveRole, tuple[list[str], int]] = {
        CognitiveRole.DECOMPOSER: (["text", "reasoning"], 8192),
        CognitiveRole.ACTOR: (["text", "tool_use"], 8192),
        CognitiveRole.MONITOR: (["text", "reasoning"], 4096),
        CognitiveRole.PREDICTOR: (["text", "reasoning"], 8192),
        CognitiveRole.EVALUATOR: (["text", "reasoning"], 4096),
    }
    capabilities, min_context = requirements[role]
    return RoleModelProfile(
        role=role,
        required_capabilities=capabilities,
        min_context_tokens=min_context,
        latency_mode=latency_mode,
    )


def parse_role_selection(
    data: dict[str, Any],
    *,
    profile: RoleModelProfile,
) -> RoleModelSelection:
    """Validate adapter output into a RoleModelSelection (fail closed).

    Malformed shapes, unknown fields, out-of-range scores, unavailable
    selections carrying model ids, and authority-shaped extras are all
    rejected — never repaired, never partially accepted."""
    payload = dict(data)
    payload.setdefault("role", profile.role.value)
    try:
        return RoleModelSelection.model_validate(payload)
    except ValidationError as exc:
        raise ModelOutputRejected(
            f"role model selection rejected: {profile.role.value}",
            detail=str(exc.errors()[:3]),
        ) from None


__all__ = [
    "MAX_SELECTION_REASONS",
    "RoleModelProfile",
    "RoleModelRouter",
    "RoleModelSelection",
    "parse_role_selection",
    "role_profile",
]
