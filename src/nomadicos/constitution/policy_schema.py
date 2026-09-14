"""Policy schema: versioned, machine-enforced policies (BP §257-258).

Policies are structured, never model-generated (BP §257). Unknown fields are
rejected; risk levels use BP §90's tool trust classification.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from nomadicos.constitution.invariants import validate_policy_against_invariants
from nomadicos.core.errors import SecurityPolicyViolation


def _fail_closed_policy(exc: ValidationError) -> SecurityPolicyViolation:
    """Uniform rejection type for invalid policy artifacts (BP §85, ADR-0013)."""
    first: dict = dict(exc.errors()[0]) if exc.errors() else {}
    loc = first.get("loc") or ()
    locator = ".".join(str(v) for v in loc) or "unknown"
    detail = str(first.get("msg", "invalid"))
    return SecurityPolicyViolation(
        f"policy rejected: {locator} ({detail})",
        context={"errors": exc.error_count()},
    )


class _StrictPolicyModel(BaseModel):
    """Strict + uniform fail-closed policy base: any schema violation of a
    policy artifact is a SecurityPolicyViolation, never a plain ValidationError."""

    model_config = ConfigDict(extra="forbid")

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise _fail_closed_policy(exc) from exc


class RiskLevel(StrEnum):
    """Tool trust classification (BP §90)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Requirement(StrEnum):
    """Approval prerequisites a policy can demand (BP §39, §182, §332-333)."""

    EXPLICIT_USER_AUTHORIZATION = "explicit_user_authorization"
    USER_CONFIRMATION = "user_confirmation"
    DRY_RUN_FIRST = "dry_run_first"
    VERIFICATION_REQUIRED = "verification_required"


PathPattern = Annotated[str, StringConstraints(min_length=1, max_length=512)]
ToolSelector = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class PathPolicy(_StrictPolicyModel):
    """Path constraints for filesystem-capable tools (BP §193)."""

    allow: list[PathPattern] = Field(default_factory=list)
    deny: list[PathPattern] = Field(default_factory=list)


class ToolPolicy(_StrictPolicyModel):
    """One tool's authorization posture (BP §39, §90).

    `requires` gates first: if an unmet explicit_user_authorization is demanded,
    the decision is DENY regardless of default. Once requirements are satisfied,
    `default_decision` is the outcome.
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9._-]{2,63}$")]
    tool: ToolSelector  # exact tool name or "*" wildcard
    risk: RiskLevel
    requires: list[Requirement] = Field(default_factory=list)
    default_decision: Literal["allow", "ask", "deny"] = "deny"
    paths: PathPolicy | None = None
    max_calls_per_task: int = Field(default=100, ge=1)
    notes: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _critical_needs_authorization(self) -> "ToolPolicy":
        if self.risk is RiskLevel.CRITICAL and (
            Requirement.EXPLICIT_USER_AUTHORIZATION not in self.requires
        ):
            raise ValueError(
                f"critical-risk policy {self.id} requires explicit_user_authorization "
                "(BP §85, §90: fail closed)"
            )
        return self


class ExternalNetworkPolicy(_StrictPolicyModel):
    """Internet is an information source only (BP §200, §268, answers Section J)."""

    allow_public_get: bool = True
    allow_private_data_upload: Literal[False] = False  # invariant I11 — not negotiable
    allow_external_model_inference: Literal[False] = False  # invariant I1 — not negotiable
    denied_domains: list[str] = Field(default_factory=list)


class OwnerPolicy(_StrictPolicyModel):
    """The owner-configurable layer (BP §261). Cannot override invariants."""

    model_config = ConfigDict(extra="forbid")

    autonomy_level: Literal["manual", "assisted", "autonomous", "full_autonomy"] = "assisted"
    tools: list[ToolPolicy] = Field(default_factory=list)
    external_network: ExternalNetworkPolicy = Field(default_factory=ExternalNetworkPolicy)

    @model_validator(mode="after")
    def _unique_tool_ids(self) -> "OwnerPolicy":
        ids = [p.id for p in self.tools]
        if len(set(ids)) != len(ids):
            raise ValueError("tool policy ids must be unique")
        validate_policy_against_invariants(self.model_dump())
        return self


class PolicyDocument(_StrictPolicyModel):
    """Top-level policy file envelope (BP §258: policies require versions)."""

    version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$")]
    owner: OwnerPolicy

    @model_validator(mode="after")
    def _post_validate(self) -> "PolicyDocument":
        validate_policy_against_invariants(self.model_dump())
        return self

    def rules_for(self, tool_name: str) -> list[ToolPolicy]:
        exact = [p for p in self.owner.tools if p.tool == tool_name]
        wildcard = [p for p in self.owner.tools if p.tool == "*"]
        return exact + wildcard


def resolve_decision(
    rules: list[ToolPolicy], risk: RiskLevel, has_user_authorization: bool
) -> Literal["allow", "ask", "deny"]:
    """Merge matching rules into one decision. Deny wins; fail closed on conflict.

    A rule that *demands* explicit user authorization produces "deny" until that
    authorization exists (BP §85). The strictest matching rule wins.
    """
    if not rules:
        return "deny"  # BP §85: unknown tool/permission → BLOCK
    decisions: list[str] = []
    for rule in rules:
        if Requirement.EXPLICIT_USER_AUTHORIZATION in rule.requires and not has_user_authorization:
            decisions.append("deny")
        else:
            decisions.append(rule.default_decision)
    if "deny" in decisions:
        return "deny"
    if "ask" in decisions:
        return "ask"
    return "allow"


__all__ = [
    "ExternalNetworkPolicy",
    "OwnerPolicy",
    "PathPolicy",
    "PolicyDocument",
    "Requirement",
    "RiskLevel",
    "ToolPolicy",
    "resolve_decision",
]
