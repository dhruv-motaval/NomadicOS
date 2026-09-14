"""Phase 3 integration: Gate + permissions + policy + audit + event spine."""

import pytest

from nomadicos.audit.base import AuditEventCategory
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.events import EventBus
from nomadicos.security.gate import Decision, SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity

POLICY = """
version: "1.0.0"
owner:
  autonomy_level: assisted
  tools:
    - id: filesystem.read
      tool: filesystem.read
      risk: low
      default_decision: allow
    - id: terminal.exec
      tool: terminal.exec
      risk: high
      default_decision: ask
  external_network:
    allow_public_get: true
"""


@pytest.fixture()
def wired(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    policy_engine = PolicyEngine()
    policy_engine.load_file(path)
    permissions = PermissionEngine()
    sink = FakeAuditSink()
    gate = SecurityGate(policy_engine, permissions, sink)

    audit_events: list = []

    async def forward(event):
        audit_events.append(event)

    bus = EventBus()
    bus.subscribe("SECURITY_EVENT", forward)
    return gate, permissions, sink, bus, audit_events


async def test_mediated_flow_allow_with_audit(wired) -> None:
    gate, permissions, sink, bus, audit_events = wired
    identity = SubjectIdentity(user_id="u", session_id="s", task_id="t", step_id="st-1")

    decision = await gate.authorize(tool="filesystem.read", risk=RiskLevel.LOW, identity=identity)
    assert decision.allowed
    assert sink.events[-1].category is AuditEventCategory.TOOL_DECISION
    assert sink.events[-1].step_id == "st-1"  # correlation reaches the audit trail

    # one-shot denial path
    decision = await gate.authorize(tool="no.rule", risk=RiskLevel.HIGH, identity=identity)
    assert decision.refused
    assert permissions.denial_count("no.rule") == 1


async def test_ask_decision_flow(wired) -> None:
    gate, permissions, sink, bus, audit_events = wired
    identity = SubjectIdentity(user_id="u")
    decision = await gate.authorize(tool="terminal.exec", risk=RiskLevel.HIGH, identity=identity)
    assert decision.decision is Decision.ASK
    # ASK is a hard policy posture: the user confirms through the ASK channel
    # (UI/CLI prompt), not by pre-granting (BP §182: confirmation is interactive).
    # The flag reports no *pre-authorization* exists for this tool.
    assert decision.requires_user_authorization is True
    assert sink.events[-1].decision == "ASK"


async def test_gate_refusal_never_raises_unless_asked(wired) -> None:
    gate, *_ = wired
    identity = SubjectIdentity(user_id="u")
    decision = await gate.authorize(tool="no.rule", risk=RiskLevel.HIGH, identity=identity)
    with pytest.raises(Exception, match="fail closed"):
        gate.raise_if_refused(decision)


async def test_permission_engine_rejects_model_actions(wired) -> None:
    """I3: the model surface can never grant permissions."""
    gate, permissions, *_ = wired
    with pytest.raises(Exception, match="owner surface"):
        permissions.assert_user_action("model")
    permissions.assert_user_action("cli")  # owner path is fine
