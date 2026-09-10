import pytest

from nomadicos.constitution.invariants import (
    INVARIANT_IDS,
    INVARIANTS,
    validate_policy_against_invariants,
)
from nomadicos.core.errors import SecurityPolicyViolation


def test_all_fifteen_invariants_defined() -> None:
    assert len(INVARIANTS) == 15
    assert len(INVARIANT_IDS) == 15
    assert {f"I{i}" for i in range(1, 16)} == INVARIANT_IDS


def test_every_invariant_cites_the_blueprint() -> None:
    for invariant in INVARIANTS:
        assert invariant.blueprint_ref.startswith("BP §")
        assert invariant.title


@pytest.mark.parametrize(
    "key",
    [
        "allow_policy_modification",
        "allow_privilege_escalation",
        "bypass_security_gate",
        "allow_external_model_inference",
        "allow_private_data_export",
        "disable_audit",
    ],
)
@pytest.mark.security
def test_policy_cannot_grant_invariant_protected_capabilities(key: str) -> None:
    with pytest.raises(SecurityPolicyViolation):
        validate_policy_against_invariants({"tools": [], key: True})


@pytest.mark.security
def test_policy_with_false_flags_is_acceptable() -> None:
    validate_policy_against_invariants(
        {"allow_policy_modification": False, "tools": [{"id": "p1"}]}
    )
