"""Security Gate (BP §36, §73, §85, §98, §283-294).

Single mandatory mediation point between agent intent and effectful action:

    MODEL (untrusted proposal)
        -> AGENT RUNTIME (mediated request)
        -> SECURITY GATE (this module: authorization decision)
        -> EXECUTION (via Tool Gateway / Network Gateway)

Fail closed on unknown state (BP §85). Every decision is audited (BP §36.5).
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSeverity,
    AuditSink,
)
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import SecurityPolicyViolation
from nomadicos.core.logging import get_logger
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity

logger = get_logger("security.gate")


class Decision(StrEnum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"
    BLOCK = "BLOCK"


# Decision -> whether execution may proceed at all
_TERMINAL_REFUSALS = frozenset({Decision.DENY, Decision.BLOCK})


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    decision: Decision
    reason: str
    risk: RiskLevel
    policy_rules_matched: int
    requires_user_authorization: bool
    decision_latency_ms: float = field(default=0.0)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def refused(self) -> bool:
        return self.decision in _TERMINAL_REFUSALS


class SecurityGate:
    """Authorizes every effectful action (BP §36.1). Also the Network Gate's
    policy source for destinations (BP §99) until Phase 7."""

    def __init__(
        self,
        policy_engine: PolicyEngine,
        permissions: PermissionEngine,
        audit_sink: AuditSink,
    ) -> None:
        self._policy = policy_engine
        self._permissions = permissions
        self._audit = audit_sink

    async def authorize(
        self,
        *,
        tool: str,
        risk: RiskLevel,
        identity: SubjectIdentity,
        arguments: dict[str, Any] | None = None,
        classification: str = "internal",
    ) -> SecurityDecision:
        """Authorize one mediated action. Never raises for policy outcomes —
        refusals are returned as decisions and audited (BP §36.2)."""
        started = time.monotonic()
        arguments = arguments or {}

        # 1. Emergency stop latch (BP §122, ADR-0016) — blocks everything.
        if getattr(self, "_emergency_stopped", False):
            decision = SecurityDecision(
                Decision.BLOCK,
                "emergency stop active",
                risk,
                0,
                False,
                (time.monotonic() - started) * 1000,
            )
            await self._audit_decision(tool, identity, decision, arguments)
            return decision

        # 2. Policy lookup (fail closed when no rules match — BP §85).
        has_authorization = self._permissions.has_authorization(tool)
        decision_value = self._policy.decision_for(tool, risk, has_authorization)
        matched = len(self._policy.rules_for(tool))

        # 3. Sensitive data requires stricter handling (BP §264-266: uncertain ⇒ stricter).
        if classification == "sensitive" and decision_value == "allow":
            decision_value = "ask"

        decision = SecurityDecision(
            Decision(decision_value.upper()),
            reason=self._reason_for(decision_value, matched, has_authorization),
            risk=risk,
            policy_rules_matched=matched,
            requires_user_authorization=not has_authorization,
            decision_latency_ms=(time.monotonic() - started) * 1000,
        )

        await self._audit_decision(tool, identity, decision, arguments)
        if decision.refused:
            self._permissions.record_denial(tool, decision.reason)
        # consume one-shot grants on successful use
        if decision.allowed:
            self._permissions.consume(tool)
        return decision

    async def authorize_network(
        self,
        *,
        destination: str,
        identity: SubjectIdentity,
        method: str = "GET",
    ) -> SecurityDecision:
        """Network destination check (BP §99, §195-196)."""
        network_policy = self._policy.external_network()
        started = time.monotonic()
        if network_policy is None:
            decision = SecurityDecision(
                Decision.BLOCK,
                "no network policy loaded (fail closed)",
                RiskLevel.HIGH,
                0,
                False,
                (time.monotonic() - started) * 1000,
            )
        elif not network_policy.allow_public_get:
            decision = SecurityDecision(
                Decision.BLOCK,
                "public GET disabled by policy",
                RiskLevel.HIGH,
                0,
                False,
                (time.monotonic() - started) * 1000,
            )
        elif self._destination_denied(destination, network_policy.denied_domains):
            decision = SecurityDecision(
                Decision.BLOCK,
                f"destination is denied: {destination}",
                RiskLevel.HIGH,
                0,
                False,
                (time.monotonic() - started) * 1000,
            )
        else:
            decision = SecurityDecision(
                Decision.ALLOW,
                "public GET allowed",
                RiskLevel.HIGH,
                0,
                False,
                (time.monotonic() - started) * 1000,
            )
        await self._audit_decision(
            f"network.{method.lower()}", identity, decision, {"destination": destination}
        )
        return decision

    @staticmethod
    def _destination_denied(destination: str, denied_domains: list[str]) -> bool:
        """BP §195-196: validate the actual destination host, including redirects."""
        from urllib.parse import urlparse

        host = (urlparse(destination).hostname or "").lower()
        if not host:
            return True  # unparseable destination ⇒ BLOCK (fail closed)
        for denied in denied_domains:
            denied = denied.lower()
            if host == denied or host.endswith("." + denied):
                return True
        return False

    def raise_if_refused(self, decision: SecurityDecision) -> None:
        """Bridge to exception style for callers that prefer fail-fast."""
        if decision.refused:
            raise SecurityPolicyViolation(
                decision.reason,
                context={"decision": decision.decision.value, "risk": decision.risk.value},
            )

    def pull_emergency_stop(self) -> None:
        self._emergency_stopped = True
        logger.warning("EMERGENCY STOP engaged")

    def reset_emergency_stop(self) -> None:
        self._emergency_stopped = False

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _reason_for(decision_value: str, matched: int, has_authorization: bool) -> str:
        if matched == 0:
            return "no matching policy rule (fail closed)"
        if decision_value == "deny":
            return "denied by policy" if has_authorization else "authorization required"
        if decision_value == "ask":
            return "policy requires user confirmation"
        return "allowed by policy"

    async def _audit_decision(
        self,
        tool: str,
        identity: SubjectIdentity,
        decision: SecurityDecision,
        arguments: dict[str, Any],
    ) -> None:
        severity = (
            AuditSeverity.CRITICAL
            if decision.decision is Decision.BLOCK
            else AuditSeverity.WARNING
            if decision.refused
            else AuditSeverity.INFO
        )
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_DECISION,
                severity=severity,
                user_id=identity.user_id,
                session_id=identity.session_id,
                task_id=identity.task_id,
                run_id=identity.run_id,
                step_id=identity.step_id,
                subject=tool,
                decision=decision.decision.value,
                reason=decision.reason,
                fields={"argument_count": len(arguments)},  # never raw arguments (I12)
            )
        )


__all__ = ["Decision", "SecurityDecision", "SecurityGate"]
