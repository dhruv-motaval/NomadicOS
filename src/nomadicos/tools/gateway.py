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
from nomadicos.core.task_ir import ActionKind, TaskAction
from nomadicos.security.budgets import TaskBudgetTracker
from nomadicos.security.capability_registry import (
    Capability,
    default_capability,
    is_managed,
    resolve,
    risk_from_spec,
)
from nomadicos.security.gate import Decision, SecurityDecision, SecurityGate
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
        from nomadicos.agent.executor import GrantRegistry, TaskExecutor

        self._gate = security_gate
        self._audit = audit_sink
        self._bus = bus
        self._tools: dict[str, Tool] = {}
        # STEP 5 split: the DISPATCH half lives in TaskExecutor, fed only by
        # AuthorizedAction tokens this gateway issues after policy ALLOW.
        self._grants = GrantRegistry()
        self.executor = TaskExecutor(self._grants, audit_sink)

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
        source these fields may come from: the registered tool + FORMAL
        CAPABILITY REGISTRY. Unknown tool or unregistered action ⇒
        PermissionDenied (fail closed, BP §85) BEFORE policy/executor sees it."""
        capability = self.resolve_capability(tool_name, arguments)
        return capability.risk, (capability.id,)

    def resolve_capability(self, tool_name: str, arguments: dict[str, Any]) -> Capability:
        """Single capability-resolution site (trust boundary)."""
        tool = self.get(tool_name)  # unknown tool ⇒ PermissionDenied
        if is_managed(tool_name):
            # explicit action contract: unknown filesystem action etc. STAYS denied
            return resolve(tool_name, arguments if isinstance(arguments, dict) else {})
        try:
            return resolve(tool_name, arguments if isinstance(arguments, dict) else {})
        except PermissionDenied:
            # owner-registered tool without registry enumeration →
            # deterministic generic contract bound to its declared risk
            return default_capability(tool_name, risk_from_spec(tool.spec.risk.value))

    async def audit_denial(
        self, tool_name: str, reason: str, identity: SubjectIdentity,
        reason_code: str = "CAPABILITY_NOT_REGISTERED",
    ) -> None:
        """Every refusal — even one that never reaches the policy engine —
        leaves an audit trail (rebuild plan §11/§18)."""
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_DECISION,
                severity=AuditSeverity.WARNING,
                user_id=identity.user_id,
                session_id=identity.session_id,
                task_id=identity.task_id,
                run_id=identity.run_id,
                step_id=identity.step_id,
                subject=tool_name,
                decision="DENY",
                reason=reason[:1024],
                fields={"reason_code": reason_code, "resource": None,
                        "capability": None, "policy_version": "pre-policy"},
            )
        )

    @staticmethod
    def describe_resource(
        tool_name: str, capability: Capability, arguments: dict[str, Any]
    ) -> str | None:
        """Small human-auditable resource label (path/command/url/script) —
        never argument dumps, never secrets (I12)."""
        if capability.resource_kind == "path":
            return str(arguments.get("path", ""))[:240] or None
        if capability.resource_kind == "command":
            command = str(arguments.get("command", ""))
            return (command[:120] + (" …" if len(command) > 120 else "")) or None
        if capability.resource_kind == "url":
            return str(arguments.get("url", ""))[:240] or None
        if capability.resource_kind == "script":
            return tool_name[:240] or None
        return None

    # --------------------------------------------------------------- execution

    async def authorize_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        identity: SubjectIdentity,
        budget: TaskBudgetTracker | None = None,
        *,
        task_ref: TaskAction | None = None,
    ) -> tuple[SecurityDecision, "Any", "ToolResult | None"]:
        """POLICY side of the STEP 5 split: the ONLY producer of executable
        AuthorizedAction tokens. Returns (decision, token-or-None, refusal-or-None);
        raises exactly as the legacy pipeline does (schema/budget fail fast)."""
        from nomadicos.agent.executor import AuthorizedAction

        started = time.monotonic()
        tool = self.get(tool_name)  # unknown tool ⇒ PermissionDenied (fail closed)

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

        # 2. Budget check (BP §72, I10) — scheduler-side, stays in the pipeline.
        if budget is not None:
            try:
                budget.check_tool_call()
            except BudgetExceeded as exc:
                await self._audit_result(
                    tool_name, identity, "BLOCKED", str(exc), started,
                    severity=AuditSeverity.WARNING,
                )
                raise

        # 3. Capability resolution (registry-only) + Security decision (BP §36).
        capability = self.resolve_capability(tool_name, arguments)
        resource = self.describe_resource(tool_name, capability, arguments)
        classification = (
            tool.classification(arguments)
            if hasattr(tool, "classification")
            else "internal"
        )
        decision = await self._gate.authorize(
            tool=tool_name,
            risk=capability.risk,
            identity=identity,
            arguments=arguments,
            capability=capability,
            resource=resource,
            classification=classification,
        )
        if decision.refused:
            return (
                decision,
                None,
                ToolResult.failure(
                    f"security gate refused: {decision.decision.value} — {decision.reason}",
                    evidence={"decision": decision.decision.value},
                ),
            )
        if decision.decision is Decision.ASK:
            # ASK flow: caller must obtain confirmation and re-submit (BP §36.2).
            return (
                decision,
                None,
                ToolResult.failure(
                    "security gate requires user confirmation (ASK)",
                    evidence={"decision": "ASK"},
                ),
            )

        # ALLOW: mint the single-use grant + token (policy ends HERE; the
        # executor cannot invent either of these).
        stamp = decision.audit_id or f"anon-{tool_name}-{time.monotonic()}"
        self._grants.issue(tool_name, stamp)
        task = task_ref or TaskAction(
            kind=ActionKind.TOOL_CALL,
            tool=tool_name,
            arguments=arguments,
            risk=capability.risk,
            capabilities=(capability.id,),
            task_id=identity.task_id or "unbound",
            step_id=identity.step_id or "unbound",
            attempt=1,
        )
        token = AuthorizedAction(
            action=task,
            tool_name=tool_name,
            capability=capability.id,
            resource=resource,
            identity=identity,
            grant_signature=stamp,
            policy_version=decision.policy_version,
            tool=tool,
        )
        return decision, token, None

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        identity: SubjectIdentity,
        budget: TaskBudgetTracker | None = None,
        *,
        dry_run: bool = False,
    ) -> ToolResult:
        """Full mediated pipeline — DEPRECATED composition retained for the
        tool-level API: authorize_action + immediate legacy dispatch.
        Refusals return failure results; only genuine tool faults raise
        (BP §241). The agent runtime does NOT use this (see _mediated_execute:
        it authorizes, then runs TaskExecutor)."""
        started = time.monotonic()
        _decision, prepared, refusal = await self.authorize_action(
            tool_name, arguments, identity, budget
        )
        assert prepared is not None or refusal is not None
        if prepared is None:
            assert refusal is not None
            return refusal

        # 4. Legacy inline dispatch (raises like the pre-STEP-5 pipeline).
        tool = prepared.tool
        context = ToolContext(
            user_id=identity.user_id,
            session_id=identity.session_id,
            task_id=identity.task_id,
            run_id=identity.run_id,
            step_id=identity.step_id,
        )
        try:
            if dry_run:
                result = await tool.dry_run(prepared.action.arguments, context)
            else:
                result = await tool.execute(prepared.action.arguments, context)
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
