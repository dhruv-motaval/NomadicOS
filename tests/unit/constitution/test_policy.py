import pytest

from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import (
    PolicyDocument,
    RiskLevel,
    ToolPolicy,
    resolve_decision,
)
from nomadicos.core.errors import SecurityPolicyViolation

VALID_POLICY = """
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
      default_decision: deny
  external_network:
    allow_public_get: true
"""


def write(tmp_path, content: str, name: str = "owner.yaml") -> object:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_policy_loads(tmp_path) -> None:
    engine = PolicyEngine()
    document = engine.load_file(write(tmp_path, VALID_POLICY))
    assert document.version == "1.0.0"
    assert len(document.owner.tools) == 2
    assert engine.versioned


def test_unknown_fields_fail_closed(tmp_path) -> None:
    engine = PolicyEngine()
    with pytest.raises(SecurityPolicyViolation):
        engine.load_file(
            write(
                tmp_path,
                VALID_POLICY + "\n  privileged_backdoor: true\n",
                "bad.yaml",
            )
        )


def test_critical_risk_requires_explicit_authorization() -> None:
    with pytest.raises(SecurityPolicyViolation, match="explicit_user_authorization"):
        ToolPolicy(
            id="fs.delete",
            tool="filesystem.delete",
            risk=RiskLevel.CRITICAL,
            requires=[],
            default_decision="allow",  # critical + allow without authorization
        )


def test_invariant_grant_in_policy_fails_closed() -> None:
    import yaml

    hostile = yaml.safe_load(
        """
version: "1.0.0"
owner:
  autonomy_level: full_autonomy
  allow_external_model_inference: true
"""
    )
    with pytest.raises(SecurityPolicyViolation):
        PolicyDocument.model_validate(hostile)


def test_missing_policy_directory_fails_closed(tmp_path) -> None:
    engine = PolicyEngine()
    with pytest.raises(SecurityPolicyViolation):
        engine.load_directory(tmp_path / "does-not-exist")


def test_empty_policy_directory_fails_closed(tmp_path) -> None:
    engine = PolicyEngine()
    with pytest.raises(SecurityPolicyViolation):
        engine.load_directory(tmp_path)


# ------------------------------------------------------------- decisions


def rule(risk=RiskLevel.LOW, requires=(), default="allow") -> ToolPolicy:
    return ToolPolicy(
        id="rule-1",
        tool="filesystem.read",
        risk=risk,
        requires=list(requires),
        default_decision=default,
    )


def test_resolve_decision_fail_closed_by_default() -> None:
    assert resolve_decision([], RiskLevel.LOW, False) == "deny"


def test_resolve_decision_deny_wins() -> None:
    rules = [rule(default="allow"), rule(default="deny")]
    assert resolve_decision(rules, RiskLevel.LOW, True) == "deny"


def test_resolve_decision_authorization_gates_critical() -> None:
    rules = [
        rule(
            risk=RiskLevel.CRITICAL,
            requires=["explicit_user_authorization"],
            default="allow",
        )
    ]
    assert resolve_decision(rules, RiskLevel.CRITICAL, False) == "deny"
    assert resolve_decision(rules, RiskLevel.CRITICAL, True) == "allow"


def test_resolve_decision_ask_when_mixed() -> None:
    rules = [rule(default="allow"), rule(default="ask")]
    assert resolve_decision(rules, RiskLevel.LOW, True) == "ask"


def test_engine_decision_delegates(tmp_path) -> None:
    engine = PolicyEngine()
    engine.load_file(write(tmp_path, VALID_POLICY))
    assert engine.decision_for("filesystem.read", RiskLevel.LOW) == "allow"
    assert engine.decision_for("unknown.tool", RiskLevel.LOW) == "deny"  # fail closed
    assert (
        engine.decision_for("filesystem.delete", RiskLevel.CRITICAL) == "deny"
    )  # needs authorization
