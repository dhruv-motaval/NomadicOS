"""Security Gate (BP §36, §73, §85, §98, §283-294).

Single mandatory mediation point between agent intent and effectful action:

    MODEL (untrusted proposal)
        -> AGENT RUNTIME (mediated request)
        -> SECURITY GATE (this module: authorization decision)
        -> EXECUTION (via Tool Gateway / Network Gateway)

Fail closed on unknown state (BP §85). Every decision is audited (BP §36.5).
"""

import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

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
from nomadicos.security.capability_registry import Capability
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity

logger = get_logger("security.gate")

_NON_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


def _non_public_reason(destination: str) -> tuple[str, str] | None:
    """Deterministic SSRF guard: only public http(s) hosts are allowed.
    (DNS-resolved private addresses remain an accepted residual risk for v0.1.)"""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(destination)
    if parsed.scheme not in ("http", "https"):
        return (f"unsupported URL scheme: {parsed.scheme or '(none)'}", "INVALID_SCHEME")
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return ("destination has no hostname", "UNPARSEABLE_DESTINATION")
    if host == "localhost" or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
        return (f"non-public destination blocked: {host}", "NON_PUBLIC_DESTINATION")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # public hostname literal: allowed pending denied_domains check
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return (f"non-public destination blocked: {host}", "NON_PUBLIC_DESTINATION")
    return None


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
    capability: str | None = None
    resource: str | None = None
    reason_code: str = "POLICY_EVALUATED"
    policy_version: str = "unversioned"
    audit_id: str = ""

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
        capability: Capability | None = None,
        resource: str | None = None,
    ) -> SecurityDecision:
        """Authorize one mediated action deterministically: registry capability
        + owner policy + user grants — never model output (BP §36.1, §257).
        Never raises for policy outcomes — refusals are decisions, audited
        (BP §36.2). Given identical inputs, the decision is identical (§4)."""
        started = time.monotonic()
        arguments = arguments or {}
        version = getattr(self._policy, "document_version", "unversioned")

        # 1. Emergency stop latch (BP §122, ADR-0016) — blocks everything.
        if getattr(self, "_emergency_stopped", False):
            decision = SecurityDecision(
                Decision.BLOCK,
                "emergency stop active",
                risk,
                0,
                False,
                (time.monotonic() - started) * 1000,
                capability=capability.id if capability else None,
                resource=resource,
                reason_code="EMERGENCY_STOP",
                policy_version=version,
            )
            decision = self._stamp(decision)
            await self._audit_decision(tool, identity, decision, arguments)
            return decision

        # 2. Registered capabilities demand explicit user authorization
        #    regardless of permissive default rules (fail closed, BP §85/§90).
        #    Policy rules and grants key on the ACTUAL tool name (so owners
        #    can write a rule for "script.<name>" specifically) — the
        #    capability supplies risk + posture, never a rule bypass.
        policy_tool = tool
        if capability is not None and not capability.tool.endswith("."):
            policy_tool = capability.tool or tool
        has_authorization = self._permissions.has_authorization(policy_tool)
        if capability is not None and capability.requires_user_authorization:
            if not has_authorization:
                decision = SecurityDecision(
                    Decision.DENY,
                    f"capability {capability.id} requires explicit user authorization",
                    capability.risk,
                    0,
                    True,
                    (time.monotonic() - started) * 1000,
                    capability=capability.id,
                    resource=resource,
                    reason_code="CAPABILITY_REQUIRES_USER_AUTHORIZATION",
                    policy_version=version,
                )
                decision = self._stamp(decision)
                await self._audit_decision(tool, identity, decision, arguments)
                self._permissions.record_denial(tool, decision.reason)
                return decision

        # 3. Policy lookup (fail closed when no rules match — BP §85).
        effective_risk = capability.risk if capability else risk
        decision_value = self._policy.decision_for(
            policy_tool, effective_risk, has_authorization
        )
        matched = len(self._policy.rules_for(policy_tool))

        # 4. Sensitive data requires stricter handling (BP §264-266: uncertain ⇒ stricter).
        reason_code = "POLICY_EVALUATED"
        if classification == "sensitive" and decision_value == "allow":
            decision_value = "ask"
            reason_code = "CLASSIFICATION_ESCALATED"

        if matched == 0:
            reason_code = "NO_MATCHING_RULE"
        elif decision_value == "deny":
            reason_code = "POLICY_DENY"
        elif decision_value == "ask":
            reason_code = reason_code if reason_code != "POLICY_EVALUATED" else "POLICY_ASK"
        elif decision_value == "allow":
            reason_code = "POLICY_ALLOW"

        decision = SecurityDecision(
            Decision(decision_value.upper()),
            reason=self._reason_for(decision_value, matched, has_authorization),
            risk=effective_risk,
            policy_rules_matched=matched,
            requires_user_authorization=not has_authorization,
            decision_latency_ms=(time.monotonic() - started) * 1000,
            capability=capability.id if capability else None,
            resource=resource,
            reason_code=reason_code,
            policy_version=version,
        )

        decision = self._stamp(decision)
        await self._audit_decision(tool, identity, decision, arguments)
        if decision.refused:
            self._permissions.record_denial(tool, decision.reason)
        # consume one-shot grants on successful use
        if decision.allowed:
            self._permissions.consume(policy_tool)
            if capability and capability.tool != policy_tool:
                self._permissions.consume(tool)
        return decision

    async def authorize_network(
        self,
        *,
        destination: str,
        identity: SubjectIdentity,
        method: str = "GET",
    ) -> SecurityDecision:
        """Network destination check (BP §99, §195-196, §200): public HTTPS
        information only — no private/loopback/link-local targets (SSRF guard,
        deterministic and policy-level: the model can never raise the bar)."""
        network_policy = self._policy.external_network()
        started = time.monotonic()
        version = getattr(self._policy, "document_version", "unversioned")

        def _block(reason: str, code: str) -> SecurityDecision:
            decision = SecurityDecision(
                Decision.BLOCK,
                reason,
                RiskLevel.HIGH,
                0,
                False,
                (time.monotonic() - started) * 1000,
                capability="network.fetch",
                resource=None,
                reason_code=code,
                policy_version=version,
            )
            return decision

        if network_policy is None:
            decision = _block("no network policy loaded (fail closed)", "NO_NETWORK_POLICY")
        elif not network_policy.allow_public_get:
            decision = _block("public GET disabled by policy", "POLICY_DENY")
        else:
            reject = _non_public_reason(destination)
            if reject:
                decision = _block(reject[0], reject[1])
            elif self._destination_denied(destination, network_policy.denied_domains):
                decision = _block(
                    f"destination is denied: {destination}", "DENIED_DOMAIN"
                )
            else:
                decision = SecurityDecision(
                    Decision.ALLOW,
                    "public GET allowed",
                    RiskLevel.MEDIUM,
                    0,
                    False,
                    (time.monotonic() - started) * 1000,
                    capability="network.fetch",
                    resource=destination[:300],
                    reason_code="POLICY_ALLOW",
                    policy_version=version,
                )
        decision = self._stamp(decision)
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

    @staticmethod
    def _stamp(decision: SecurityDecision) -> SecurityDecision:
        return replace(decision, audit_id=str(uuid4()))

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
                event_id=UUID(decision.audit_id) if decision.audit_id else uuid4(),
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
                fields={
                    "argument_count": len(arguments),  # never raw arguments (I12)
                    "capability": decision.capability,
                    "resource": decision.resource,
                    "reason_code": decision.reason_code,
                    "policy_version": decision.policy_version,
                },
            )
        )


__all__ = ["Decision", "SecurityDecision", "SecurityGate"]
