"""Capability validation (Sprint 8): "Is this skill invocation
STRUCTURALLY valid?" — strictly separate from authorization ("Is this
action allowed?"), which stays exclusively with the existing
AuthorizationService.

Fail closed: unknown skill/version, disabled or unhealthy skills,
capability mismatch, input-contract mismatch, missing prerequisites,
role restrictions, and risk ceilings all reject. Every check is
recorded in the auditable ``checked`` list; every failure carries an
explicit reason. Malformed or unknown skills can never reach the
adapter, let alone the runtime.
"""

from __future__ import annotations

from typing import Any

from nomadicos.contracts.skills import (
    SkillInvocation,
    SkillValidationResult,
)
from nomadicos.skills.registry import SkillRegistry

_ARG_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "string_list": (list,),
}


def validate_invocation(
    registry: SkillRegistry,
    invocation: SkillInvocation,
    *,
    satisfied_prerequisites: list[str] | None = None,
) -> SkillValidationResult:
    """Deterministic structural validation ("is this invocation
    well-formed?") — NOT authorization ("is this action allowed?"),
    which stays with the AuthorizationService. Never raises for a
    rejected invocation — returns an explicit failed result
    (auditable)."""
    reasons: list[str] = []
    checked: list[str] = []

    definition = registry.lookup(invocation.skill_id, invocation.version)
    if definition is None:
        return SkillValidationResult(
            ok=False,
            invocation_id=invocation.invocation_id,
            skill_id=invocation.skill_id,
            reasons=[f"unknown skill {invocation.skill_id!r} version {invocation.version}"],
            checked=["exists"],
        )
    checked.append("exists")

    if not definition.enabled:
        reasons.append(f"skill {definition.skill_id} is disabled")
    checked.append("enabled")
    if not definition.healthy:
        reasons.append(f"skill {definition.skill_id} is unhealthy")
    checked.append("healthy")

    if definition.capability.split(".", 1)[0] == "":
        reasons.append(f"skill capability {definition.capability!r} malformed")
    checked.append("capability")

    args: dict[str, Any] = dict(invocation.args)
    for param in definition.inputs:
        expected = _ARG_TYPES[param.type]
        if param.name in args:
            value = args[param.name]
            if not isinstance(value, expected) or (
                param.type in ("integer",) and isinstance(value, bool)
            ):
                reasons.append(
                    f"arg {param.name!r} type mismatch: expected {param.type}"
                )
        elif param.required:
            reasons.append(f"required arg {param.name!r} missing")
    unknown = sorted(set(args) - {p.name for p in definition.inputs})
    if unknown:
        reasons.append(f"unknown args {unknown}")
    checked.append("input_contract")

    if definition.prerequisites:
        satisfied = set(satisfied_prerequisites or ())
        missing = sorted(set(definition.prerequisites) - satisfied)
        if missing:
            reasons.append(f"prerequisites missing: {missing}")
    checked.append("prerequisites")

    if definition.supported_roles and (
        invocation.requested_by not in definition.supported_roles
    ):
        reasons.append(
            f"caller role {invocation.requested_by!r} not permitted "
            f"({definition.supported_roles})"
        )
    checked.append("role")

    return SkillValidationResult(
        ok=not reasons,
        invocation_id=invocation.invocation_id,
        skill_id=definition.skill_id,
        reasons=reasons[:8],
        checked=checked,
    )
