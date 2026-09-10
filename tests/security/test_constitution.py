"""Security smoke suite: the Constitution layer cannot be bypassed by config (BP §294)."""

import pytest

from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import PolicyDocument
from nomadicos.core.errors import SecurityPolicyViolation


@pytest.mark.security
@pytest.mark.parametrize(
    "grant",
    [
        {"allow_policy_modification": True},
        {"allow_config_modification": True},
        {"allow_privilege_escalation": True},
        {"bypass_security_gate": "yes"},
        {"bypass_tool_gateway": True},
        {"allow_external_model_inference": True},
        {"allow_private_data_export": "true"},
        {"disable_invariants": True},
        {"unbounded_execution": True},
    ],
)
def test_no_policy_document_can_grant_invariant_protected_capabilities(grant) -> None:
    with pytest.raises(SecurityPolicyViolation):
        PolicyDocument.model_validate(
            {"version": "1.0.0", "owner": {"autonomy_level": "full_autonomy", **grant}}
        )


@pytest.mark.security
def test_policy_engine_never_serves_decisions_without_policy() -> None:
    engine = PolicyEngine()
    # No documents loaded: every decision must be DENY (fail closed, BP §85).
    from nomadicos.constitution.policy_schema import RiskLevel

    assert engine.decision_for("filesystem.read", RiskLevel.LOW) == "deny"
    assert engine.decision_for("anything.else", RiskLevel.CRITICAL) == "deny"
