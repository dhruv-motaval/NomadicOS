"""Security Gate tests: fail-closed, mediation, audit, emergency stop (BP §36, §85, §98)."""

import pytest

from nomadicos.audit.base import AuditSeverity
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import SecurityPolicyViolation
from nomadicos.security.gate import Decision, SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity

BASE_POLICY = """
version: "1.0.0"
owner:
  autonomy_level: assisted
  tools:
    - id: filesystem.read
      tool: filesystem.read
      risk: low
      default_decision: allow
    - id: filesystem.delete
      tool: filesystem.delete
      risk: critical
      requires: [explicit_user_authorization]
      default_decision: allow
  external_network:
    allow_public_get: true
    denied_domains: ["evil.example", "exfil.example"]
"""


@pytest.fixture()
def identity() -> SubjectIdentity:
    return SubjectIdentity(user_id="user-1", session_id="s-1", task_id="t-1")


@pytest.fixture()
def gate(tmp_path, identity) -> SecurityGate:
    path = tmp_path / "policy.yaml"
    path.write_text(BASE_POLICY, encoding="utf-8")
    engine = PolicyEngine()
    engine.load_file(path)
    return SecurityGate(engine, PermissionEngine(), FakeAuditSink())


async def test_known_tool_allowed(gate: SecurityGate, identity) -> None:
    decision = await gate.authorize(tool="filesystem.read", risk=RiskLevel.LOW, identity=identity)
    assert decision.allowed is True
    assert decision.decision is Decision.ALLOW


async def test_unknown_tool_fails_closed(gate: SecurityGate, identity) -> None:
    """BP §85: unknown tool ⇒ BLOCK."""
    decision = await gate.authorize(tool="mystery.exec", risk=RiskLevel.HIGH, identity=identity)
    assert decision.refused is True
    assert "fail closed" in decision.reason


async def test_critical_tool_denied_without_authorization(gate: SecurityGate, identity) -> None:
    decision = await gate.authorize(
        tool="filesystem.delete", risk=RiskLevel.CRITICAL, identity=identity
    )
    assert decision.refused is True
    assert decision.requires_user_authorization is True


async def test_critical_tool_allowed_with_explicit_grant(
    gate: SecurityGate, tmp_path, identity
) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(BASE_POLICY, encoding="utf-8")
    engine = PolicyEngine()
    engine.load_file(path)
    permissions = PermissionEngine()
    gate = SecurityGate(engine, permissions, FakeAuditSink())

    permissions.grant("filesystem.delete", granted_by="cli")  # owner action
    decision = await gate.authorize(
        tool="filesystem.delete", risk=RiskLevel.CRITICAL, identity=identity
    )
    assert decision.allowed is True


async def test_one_shot_grant_consumed_after_use(tmp_path, identity) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(BASE_POLICY, encoding="utf-8")
    engine = PolicyEngine()
    engine.load_file(path)
    permissions = PermissionEngine()
    gate = SecurityGate(engine, permissions, FakeAuditSink())
    permissions.grant("filesystem.delete", granted_by="cli", one_shot=True)

    first = await gate.authorize(
        tool="filesystem.delete", risk=RiskLevel.CRITICAL, identity=identity
    )
    assert first.allowed is True
    second = await gate.authorize(
        tool="filesystem.delete", risk=RiskLevel.CRITICAL, identity=identity
    )
    assert second.refused is True  # BP §355: one-shot override


@pytest.mark.security
async def test_emergency_stop_blocks_everything(gate: SecurityGate, identity) -> None:
    """BP §122/§288: stop halts execution without model cooperation."""
    gate.pull_emergency_stop()
    decision = await gate.authorize(tool="filesystem.read", risk=RiskLevel.LOW, identity=identity)
    assert decision.decision is Decision.BLOCK
    gate.reset_emergency_stop()
    decision = await gate.authorize(tool="filesystem.read", risk=RiskLevel.LOW, identity=identity)
    assert decision.allowed is True


@pytest.mark.security
async def test_sensitive_data_upgrades_allow_to_ask(gate: SecurityGate, identity) -> None:
    decision = await gate.authorize(
        tool="filesystem.read",
        risk=RiskLevel.LOW,
        identity=identity,
        classification="sensitive",
    )
    assert decision.decision is Decision.ASK


@pytest.mark.security
async def test_every_decision_is_audited(gate: SecurityGate, identity) -> None:
    """BP §36.5: all decisions audited; refusals carry warning/critical severity."""
    await gate.authorize(tool="filesystem.read", risk=RiskLevel.LOW, identity=identity)
    await gate.authorize(tool="unknown.tool", risk=RiskLevel.HIGH, identity=identity)

    sink = gate._audit
    assert isinstance(sink, FakeAuditSink)
    assert len(sink.events) == 2
    refused = [e for e in sink.events if e.decision == "DENY"]
    assert len(refused) == 1
    assert refused[0].severity is AuditSeverity.WARNING
    # no raw arguments in audit fields (I12)
    assert all("arguments" not in e.fields for e in sink.events)


@pytest.mark.security
async def test_denials_are_tracked_for_escalation(gate: SecurityGate, identity) -> None:
    for _ in range(3):
        await gate.authorize(tool="unknown.tool", risk=RiskLevel.HIGH, identity=identity)
    assert gate._permissions.denial_count("unknown.tool") == 3


# ------------------------------------------------------------- network policy


async def test_network_destination_allow(gate: SecurityGate, identity) -> None:
    decision = await gate.authorize_network(
        destination="https://docs.example.com", identity=identity
    )
    assert decision.allowed is True


async def test_network_denied_domain_blocked(gate: SecurityGate, identity) -> None:
    """BP §195: destination validation."""
    decision = await gate.authorize_network(
        destination="https://evil.example/payload", identity=identity
    )
    assert decision.decision is Decision.BLOCK


async def test_network_blocked_without_policy(identity) -> None:
    gate = SecurityGate(PolicyEngine(), PermissionEngine(), FakeAuditSink())
    decision = await gate.authorize_network(
        destination="https://docs.example.com", identity=identity
    )
    assert decision.decision is Decision.BLOCK  # fail closed: no policy loaded


# ----------------------------------------------------------- policy tampering


@pytest.mark.security
def test_gate_refuses_to_construct_with_permissive_policy(tmp_path) -> None:
    """The schema layer refuses invariant-violating policy files (BP §85)."""
    path = tmp_path / "hostile.yaml"
    path.write_text(
        'version: "1.0.0"\nowner:\n  allow_policy_modification: true\n',
        encoding="utf-8",
    )
    engine = PolicyEngine()
    with pytest.raises(SecurityPolicyViolation):
        engine.load_file(path)
