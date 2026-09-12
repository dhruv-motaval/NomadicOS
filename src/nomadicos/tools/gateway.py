"""Tool Gateway (BP §11-12, §98): the ONLY execution path for tools.

Pipeline (BP §98): schema validate → identity resolve → permission check →
risk check → budget check → security decision → audit → execute → audit result.
No component may execute a tool without passing through here (I5).
"""

import time
from typing import Any

from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSeverity,
    AuditSink,
)
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import (
    BudgetExceeded,
    PermissionDenied,
    SecurityPolicyViolation,
    ToolExecutionError,
)
from nomadicos.core.events import EventBus, TraceContext
from nomadicos.core.logging import get_logger
from nomadicos.security.budgets import TaskBudgetTracker
from nomadicos.security.gate import Decision, SecurityGate
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import Tool, ToolContext, ToolResult

logger = get_logger("tools.gateway")


class ToolGateway:
    """Registry + mediated execution. One-stop enforcement (BP §12)."""

    def __init__(
        self,
        security_gate: SecurityGate,
        audit_sink: AuditSink,
        bus: EventBus | None = None,
    ) -> None:
        self._gate = security_gate
        self._audit = audit_sink
        self._bus = bus
        self._tools: dict[str, Tool] = {}

    # ---------------------------------------------------------------- registry

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool
        logger.info("tool registered name=%s risk=%s", name, tool.spec.risk.value)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            # Unknown tool ⇒ refuse to even look at it (BP §85, I5).
            raise PermissionDenied(
                f"unknown tool: {name}",
                context={"registered": sorted(self._tools)},
            )
        return self._tools[name]

    def registered_tools(self) -> list[str]:
        return sorted(self._tools)

    def action_descriptor(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> tuple[RiskLevel, tuple[str, ...]]:
        """SYSTEM-derived (risk, capabilities) for IR binding — the ONLY
        source these fields may come from. Unknown action ⇒ PermissionDenied
        (fail closed, BP §85) BEFORE the claim can reach policy/executor."""
        tool = self.get(tool_name)  # unknown ⇒ PermissionDenied
        action_arg = arguments.get("action") if isinstance(arguments, dict) else None
        capability = (
            f"{tool_name}.{action_arg}"
            if isinstance(action_arg, str) and action_arg
            else f"{tool_name}.invoke"
        )
        return self._risk_of(tool), (capability,)

    # --------------------------------------------------------------- execution

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        identity: SubjectIdentity,
        budget: TaskBudgetTracker | None = None,
        *,
        dry_run: bool = False,
    ) -> ToolResult:
        """Full mediated pipeline. Refusals return failure results and are
        audited; only genuine tool faults raise (BP §241)."""
        started = time.monotonic()
        tool = self.get(tool_name)  # unknown tool ⇒ PermissionDenied (fail closed)
        context = ToolContext(
            user_id=identity.user_id,
            session_id=identity.session_id,
            task_id=identity.task_id,
            run_id=identity.run_id,
            step_id=identity.step_id,
        )

        # 1. Schema validation — reject, never guess (BP §142).
        try:
            arguments = await tool.validate_arguments(arguments)
        except Exception as exc:
            await self._audit_result(
                tool_name, identity, "REJECTED", "schema violation", started,
                severity=AuditSeverity.WARNING,
            )
            raise ToolExecutionError(
                f"invalid arguments for {tool_name}: {exc}",
                context={"tool": tool_name},
            ) from exc

        # 2. Budget check (BP §72, I10).
        if budget is not None:
            try:
                budget.check_tool_call()
            except BudgetExceeded as exc:
                await self._audit_result(
                    tool_name, identity, "BLOCKED", str(exc), started,
                    severity=AuditSeverity.WARNING,
                )
                raise

        # 3. Security decision (BP §36).
        decision = await self._gate.authorize(
            tool=tool_name,
            risk=self._risk_of(tool),
            identity=identity,
            arguments=arguments,
        )
        if decision.refused:
            return ToolResult.failure(
                f"security gate refused: {decision.decision.value} — {decision.reason}",
                evidence={"decision": decision.decision.value},
            )
        if decision.decision is Decision.ASK:
            # ASK flow: the caller must obtain confirmation and re-submit with
            # a grant. Execution does not proceed on an unconfirmed ASK.
            return ToolResult.failure(
                "security gate requires user confirmation (ASK)",
                evidence={"decision": "ASK"},
            )

        # 4. Execute (or dry-run first per policy — BP §139).
        try:
            if dry_run:
                result = await tool.dry_run(arguments, context)
            else:
                result = await tool.execute(arguments, context)
        except Exception as exc:
            await self._audit_result(
                tool_name, identity, "FAILED", str(exc), started,
                severity=AuditSeverity.WARNING,
            )
            raise ToolExecutionError(
                f"tool {tool_name} failed: {exc}",
                context={"tool": tool_name},
            ) from exc

        await self._audit_result(
            tool_name, identity,
            "DRY_RUN" if dry_run else "EXECUTED",
            None, started,
            severity=AuditSeverity.INFO,
        )
        await self._emit("TOOL_EXECUTED", identity, tool_name, result.success)
        return result

    # ------------------------------------------------------------------ audit

    async def _audit_result(
        self,
        tool_name: str,
        identity: SubjectIdentity,
        outcome: str,
        detail: str | None,
        started: float,
        severity: AuditSeverity = AuditSeverity.INFO,
    ) -> None:
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_EXECUTED,
                severity=severity,
                user_id=identity.user_id,
                session_id=identity.session_id,
                task_id=identity.task_id,
                run_id=identity.run_id,
                step_id=identity.step_id,
                subject=tool_name,
                decision=outcome,
                reason=detail,
                fields={"latency_ms": round((time.monotonic() - started) * 1000, 1)},
            )
        )

    async def _emit(
        self, event_type: str, identity: SubjectIdentity, tool_name: str, success: bool
    ) -> None:
        if self._bus is None:
            return
        from nomadicos.core.events import Event

        await self._bus.publish(
            Event(
                type=event_type,
                context=TraceContext(
                    request_id=identity.user_id,  # spine is caller-owned in later phases
                    user_id=identity.user_id,
                    session_id=identity.session_id,
                    task_id=identity.task_id,
                    run_id=identity.run_id,
                    step_id=identity.step_id,
                ),
                payload={"tool": tool_name, "success": success},
            )
        )

    @staticmethod
    def _risk_of(tool: Tool):
        from nomadicos.constitution.policy_schema import RiskLevel

        mapping = {
            "read_only": RiskLevel.LOW,
            "state_changing": RiskLevel.MEDIUM,
            "destructive": RiskLevel.CRITICAL,
            "network": RiskLevel.MEDIUM,
            "administrative": RiskLevel.HIGH,
            "security_critical": RiskLevel.CRITICAL,
        }
        return mapping[tool.spec.risk.value]


def raise_if_violation(exc: Exception) -> None:
    """Fail-closed bridge: security violations are never swallowed (BP §83)."""
    if isinstance(exc, SecurityPolicyViolation):
        raise exc


__all__ = ["ToolGateway", "raise_if_violation"]
