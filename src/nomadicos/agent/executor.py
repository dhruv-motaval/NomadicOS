"""STEP 5 — the task executor boundary.

The executor is deliberately BORING: it takes ONLY an {@link AuthorizedAction}
token that the ToolGateway issued after a successful policy ALLOW, dispatches
it to the registered tool, and reports a structured {@link ExecutionResult}.

Structural guarantees (enforced, not just documented):
- ``TaskExecutor`` knows NOTHING of policy (no gate import), models
  (no GenerateRequest/LLM), budgets, retry policy, capability registry, or
  task lifecycle (no TaskState/TaskStatus).
- A token is single-use: its grant stamp is consumed on run, so nothing can
  be replayed or hand-constructed into power (unissued stamps are rejected).
- The executor never decides task state; it merely reports evidence. The
  scheduler/lifecycle layer turns results into transitions.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSeverity,
    AuditSink,
)
from nomadicos.core.errors import PermissionDenied
from nomadicos.core.task_ir import TaskAction
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import ToolContext, ToolResult

_MAX_GRANTS = 256


class GrantRegistry:
    """Single-use grant stamps issued by the gateway's policy pipeline.

    An AuthorizedAction is only executable while its stamp is present here;
    it is REMOVED at run time, so nothing can be replayed (and a hand-built
    token with a made-up stamp can never run)."""

    def __init__(self) -> None:
        self._issued: OrderedDict[str, str] = OrderedDict()

    def issue(self, tool_name: str, stamp: str) -> None:
        self._issued[stamp] = tool_name
        while len(self._issued) > _MAX_GRANTS:
            self._issued.popitem(last=False)

    def verify(self, stamp: str, tool_name: str) -> bool:
        return self._issued.get(stamp) == tool_name

    def consume(self, stamp: str) -> None:
        self._issued.pop(stamp, None)


@dataclass(frozen=True, slots=True)
class AuthorizedAction:
    """A policy-approved, capability-bound, single-use execution token.

    Only construct via ``ToolGateway.authorize_action`` — the grant stamp is
    registered there; TaskExecutor.run refuses any token whose stamp the
    gateway never issued (so raw model data cannot fabricate one)."""

    action: TaskAction
    tool_name: str
    capability: str
    resource: str | None
    identity: SubjectIdentity
    grant_signature: str
    policy_version: str
    tool: Any = field(repr=False, compare=False)  # registered Tool object


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Structured, JSON-serializable outcome — reports facts, never verdicts
    about the TASK. Lifecycle ownership: agent runtime / scheduler only."""

    success: bool
    action: str
    capability: str
    task_id: str
    step_id: str
    attempt: int
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    observations: tuple[str, ...] = ()
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "action": self.action,
            "capability": self.capability,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "data": self.data,
            "error": self.error,
            "evidence": self.evidence,
            "observations": list(self.observations),
            "duration_ms": self.duration_ms,
        }


class TaskExecutor:
    """Dispatch + evidence collection + ACTION_* audit. No policy, no model,
    no budgets, no lifecycle transitions."""

    def __init__(
        self,
        grants: GrantRegistry,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._grants = grants
        self._audit = audit_sink

    @staticmethod
    def _reject(prepared: object, why: str) -> PermissionDenied:
        return PermissionDenied(
            f"executor boundary violated: {why}",
            context={"got": type(prepared).__name__},
        )

    async def run(self, prepared: object, *, dry_run: bool = False) -> ExecutionResult:
        """Execute ONLY a gateway-issued AuthorizedAction. Anything else —
        raw dict/str/TaskAction/tokens with forged or consumed stamps —
        fails closed WITHOUT touching the tool."""
        if not isinstance(prepared, AuthorizedAction):
            raise self._reject(prepared, "only AuthorizedAction is executable")
        if not self._grants.verify(prepared.grant_signature, prepared.tool_name):
            raise PermissionDenied(
                "executor boundary violated: grant stamp was never issued "
                f"or is already used (tool={prepared.tool_name!r})",
                context={"tool": prepared.tool_name},
            )
        task = prepared.action
        started = time.monotonic()
        context = ToolContext(
            user_id=prepared.identity.user_id,
            session_id=prepared.identity.session_id,
            task_id=task.task_id,
            run_id=prepared.identity.run_id,
            step_id=task.step_id,
        )
        # single-use: consumed BEFORE dispatch — a failing tool never keeps a
        # replayable grant; retries re-authorize through policy (plan §7).
        self._grants.consume(prepared.grant_signature)
        try:
            result: ToolResult = (
                await prepared.tool.dry_run(prepared.action.arguments, context)
                if dry_run
                else await prepared.tool.execute(prepared.action.arguments, context)
            )
        except Exception as exc:  # tool faults become structured failures
            await self._audit_dispatch(
                prepared,
                "ACTION_FAILED",
                str(exc),
                started,
                severity=AuditSeverity.WARNING,
            )
            return ExecutionResult(
                success=False,
                action=prepared.tool_name,
                capability=prepared.capability,
                task_id=task.task_id,
                step_id=task.step_id,
                attempt=task.attempt,
                error=str(exc),
                error_code=str(getattr(exc, "context", {}).get("code") or "TOOL_EXCEPTION"),
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        observations = _observe(prepared, result)
        await self._audit_dispatch(
            prepared,
            "EXECUTED" if result.success else "ACTION_FAILED",
            result.error,
            started,
            severity=AuditSeverity.INFO if result.success else AuditSeverity.WARNING,
        )
        return ExecutionResult(
            success=result.success,
            action=prepared.tool_name,
            capability=prepared.capability,
            task_id=task.task_id,
            step_id=task.step_id,
            attempt=task.attempt,
            data=result.data,
            error=result.error,
            error_code=None if result.success else (result.error_code or "TOOL_FAILURE"),
            evidence=dict(result.evidence),
            observations=observations,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    async def _audit_dispatch(
        self,
        prepared: AuthorizedAction,
        decision: str,
        detail: str | None,
        started: float,
        severity: AuditSeverity,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_EXECUTED,
                severity=severity,
                user_id=prepared.identity.user_id,
                session_id=prepared.identity.session_id,
                task_id=prepared.action.task_id,
                run_id=prepared.identity.run_id,
                step_id=prepared.action.step_id,
                subject=prepared.tool_name,
                decision=decision,
                reason=(detail or "")[:1024] or None,
                fields={
                    "capability": prepared.capability,
                    "resource": prepared.resource,
                    "attempt": prepared.action.attempt,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
        )


def _observe(prepared: AuthorizedAction, result: ToolResult) -> tuple[str, ...]:
    """Small structural observations (never raw content dumps, I12)."""
    notes = [f"tool={prepared.tool_name}", f"capability={prepared.capability}"]
    if prepared.resource:
        notes.append(f"target={prepared.resource}")
    notes.append(f"returned_success={result.success}")
    return tuple(notes)


__all__ = [
    "AuthorizedAction",
    "ExecutionResult",
    "GrantRegistry",
    "TaskExecutor",
]
