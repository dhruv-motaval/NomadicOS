"""Skill adapter (Sprint 8): the NARROW boundary between a skill and the
existing Action IR.

    SkillInvocation → ActionProposal

…then the EXISTING runtime is the ONLY execution path:

    ActionProposal → ProposalValidator → AuthorizationService →
    Executor → ExecutionResult → Verifier

The adapter is a pure data translator: it NEVER authorizes (no
authority fields exist on a proposal — the closed schema plus the
Action-IR parser reject them structurally), NEVER executes, NEVER
calls the validator/authorization/executor on its own, and NEVER
bypasses capability validation. It cannot create an AuthorizedAction
— only the authorization subsystem can.
"""

from __future__ import annotations

from nomadicos.contracts.action import ActionProposal, ProposalKind
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.skills import SkillDefinition, SkillInvocation, SkillResult


def parse_capability(capability: str) -> tuple[str, str]:
    """Split "tool.operation" deterministically; malformed fails closed."""
    tool, dot, operation = capability.partition(".")
    if not dot or not tool.strip() or not operation.strip():
        raise ValueError(f"malformed capability {capability!r}")
    return tool, operation


def to_proposal(
    invocation: SkillInvocation,
    definition: SkillDefinition,
    *,
    model_id: str,
    task_id: str,
    step_id: str,
    attempt: int = 1,
) -> ActionProposal:
    """Translate a validated invocation into a NORMAL ActionProposal.

    The proposal carries the skill's capability and args plus a bounded
    data-only note; it must pass the same ProposalValidator,
    AuthorizationService, and Executor path as any other proposal. A
    capability mismatch between invocation and definition fails closed."""
    tool, operation = parse_capability(definition.capability)
    if definition.skill_id != invocation.skill_id:
        raise ValueError("invocation does not match the resolved skill definition")
    note = f"skill={definition.skill_id}@v{definition.version} risk={definition.risk.value}"
    return ActionProposal(
        task_id=task_id,
        step_id=step_id,
        attempt=attempt,
        model_id=model_id,
        kind=ProposalKind.ACTION,
        tool=tool,
        operation=operation,
        args=dict(invocation.args),
        note=note[:200],
    )


def to_result(
    execution: ExecutionResult,
    invocation: SkillInvocation,
    definition: SkillDefinition,
) -> SkillResult:
    """Map an EXISTING ExecutionResult into a structured SkillResult.

    Pure data mapping: status/evidence flow back to the brain through
    the same observation/evidence paths as every other execution; the
    result NEVER mutates BrainState by itself."""
    return SkillResult(
        skill_id=definition.skill_id,
        version=definition.version,
        invocation_id=invocation.invocation_id,
        status=execution.status,
        execution_id=execution.id,
        evidence={str(k)[:64]: v for k, v in list(execution.evidence.items())[:16]},
        failure_category=execution.failure.name if execution.failure is not None else "",
        message=(execution.message or "")[:500],
    )
