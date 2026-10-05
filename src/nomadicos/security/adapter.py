"""Security adapter (Sprint 9): SecurityAction → ActionProposal.

The NARROW translation into the EXISTING authoritative path — after it,
nothing is security-specific anymore:

    ActionProposal → ProposalValidator → AuthorizationService →
    Executor → ExecutionResult → Verifier

The adapter NEVER authorizes, NEVER executes, NEVER calls the validator
or authorization service itself, and can NEVER mint an AuthorizedAction
— only the authorization subsystem can. Capability → tool.operation
mapping is a fixed, closed table over EXISTING tool capabilities (no new
tools, no second executor).
"""

from __future__ import annotations

from nomadicos.contracts.action import ActionProposal, ProposalKind
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.security import (
    Engagement,
    SandboxRecord,
    SecurityAction,
    SecurityCapabilityKind,
    TargetRecord,
)

#: closed mapping from security capability kinds to EXISTING tool
#: operations (Sprint 8 taxonomy style — no new tools, no arbitrary
#: command strings; the validator/authorization path governs everything)
_CAPABILITY_TOOL: dict[str, tuple[str, str]] = {
    SecurityCapabilityKind.RECON.value: ("filesystem", "exists"),
    SecurityCapabilityKind.ANALYSIS.value: ("filesystem", "read"),
    SecurityCapabilityKind.CONFIGURATION_REVIEW.value: ("filesystem", "read"),
    SecurityCapabilityKind.CODE_ANALYSIS.value: ("filesystem", "read"),
    SecurityCapabilityKind.DEPENDENCY_ANALYSIS.value: ("filesystem", "read"),
    SecurityCapabilityKind.VULNERABILITY_VALIDATION.value: ("terminal", "execute"),
    SecurityCapabilityKind.EVIDENCE_COLLECTION.value: ("filesystem", "read"),
}


def capability_tool(capability: SecurityCapabilityKind) -> tuple[str, str]:
    """Deterministic capability → existing tool.operation mapping."""
    return _CAPABILITY_TOOL[capability.value]


def to_action_proposal(
    action: SecurityAction,
    engagement: Engagement,
    target: TargetRecord,
    sandbox: SandboxRecord,
    *,
    model_id: str,
    task_id: str,
    step_id: str,
    attempt: int = 1,
) -> ActionProposal:
    """Translate a validated SecurityAction into a NORMAL ActionProposal.

    Bindings are re-checked (fail closed): engagement/target/sandbox ids
    must match exactly — no target/capability/sandbox substitution. The
    result is an untrusted proposal like any other: the existing
    validator, authorization service, and executor remain authoritative."""
    if engagement.engagement_id != action.engagement_id:
        raise ValueError("engagement binding mismatch")
    if target.target_id != action.target_id:
        raise ValueError("target binding mismatch")
    if sandbox.sandbox_id != action.sandbox_id:
        raise ValueError("sandbox binding mismatch")
    tool, operation = capability_tool(action.capability)
    if f"{tool}.{operation}" not in target.allowed_operations:
        raise ValueError(
            f"target {target.target_id!r} does not allow {tool}.{operation}"
        )
    note = (
        f"security={action.capability.value} engagement={engagement.engagement_id} "
        f"target={target.target_id} sandbox={sandbox.sandbox_id}"
    )
    return ActionProposal(
        task_id=task_id,
        step_id=step_id,
        attempt=attempt,
        model_id=model_id,
        kind=ProposalKind.ACTION,
        tool=tool,
        operation=operation,
        args=dict(action.args),
        note=note[:200],
    )


def to_security_result(execution: ExecutionResult, action: SecurityAction) -> dict[str, object]:
    """Map an existing ExecutionResult into structured security data."""
    return {
        "capability": action.capability.value,
        "invocation_id": action.invocation_id,
        "engagement_id": action.engagement_id,
        "target_id": action.target_id,
        "execution_id": execution.id,
        "status": execution.status.value,
        "evidence": {
            str(key)[:64]: value
            for key, value in list(execution.evidence.items())[:16]
        },
    }
