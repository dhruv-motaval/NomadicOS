"""Sprint 9 — engagements: explicit lifecycle, capability/target scope,
bounded contracts, fail-closed status handling."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.security import (
    SECURITY_CAPABILITIES,
    Engagement,
    EngagementStatus,
    SandboxClass,
    SandboxPolicy,
    SecurityCapabilityKind,
    SecurityCapabilitySpec,
    transition_engagement,
)
from nomadicos.security.engagement import validate_engagement


def engagement(**overrides) -> Engagement:
    base = dict(
        engagement_id="eng_lab",
        owner_reference="owner",
        purpose="audit the sample repository",
        allowed_targets=["tgt_repo", "tgt_app"],
        allowed_capabilities=["RECON", "CODE_ANALYSIS", "EVIDENCE_COLLECTION"],
        sandbox_policy=SandboxPolicy(),
        status=EngagementStatus.ACTIVE,
    )
    base.update(overrides)
    return Engagement(**base)


def test_engagement_contract_is_bounded_and_closed() -> None:
    assert engagement().max_actions == 10
    with pytest.raises(ValidationError):
        Engagement(**{**engagement().model_dump(mode="json"), "authorized": True})
    with pytest.raises(ValidationError):
        engagement(engagement_id="x" * 65)
    with pytest.raises(ValidationError):
        engagement(purpose="d" * 501)
    with pytest.raises(ValidationError):
        engagement(allowed_targets=[f"t{i}" for i in range(17)])
    with pytest.raises(ValidationError):
        engagement(max_actions=0)


def test_unknown_capability_rejected_in_engagement() -> None:
    with pytest.raises(ValidationError):
        engagement(allowed_capabilities=["EXPLOIT_GENERATION"])
    with pytest.raises(ValidationError):
        engagement(allowed_capabilities=["not-a-capability"])


def test_engagement_metadata_rejects_secrets() -> None:
    with pytest.raises(ValidationError):
        engagement(purpose="test password=hunter2 flow")
    with pytest.raises(ValidationError):
        engagement(owner_reference="token: abc123")


def test_only_active_engagements_pass() -> None:
    ok, reasons = validate_engagement(engagement(), SecurityCapabilityKind.RECON, "tgt_repo")
    assert ok and reasons == []
    for status in (
        EngagementStatus.DRAFT,
        EngagementStatus.SUSPENDED,
        EngagementStatus.COMPLETED,
        EngagementStatus.REVOKED,
    ):
        ok, reasons = validate_engagement(
            engagement(status=status), SecurityCapabilityKind.RECON, "tgt_repo"
        )
        assert not ok
        assert any(status.value in r for r in reasons)


def test_missing_engagement_fails_closed() -> None:
    ok, reasons = validate_engagement(None, SecurityCapabilityKind.RECON, "tgt_repo")
    assert not ok
    assert "missing engagement" in reasons[0]


def test_capability_and_target_scope_enforced() -> None:
    # capability not granted
    ok, reasons = validate_engagement(
        engagement(), SecurityCapabilityKind.DEPENDENCY_ANALYSIS, "tgt_repo"
    )
    assert not ok and any("not granted" in r for r in reasons)
    # target out of scope
    ok, reasons = validate_engagement(engagement(), SecurityCapabilityKind.RECON, "tgt_ghost")
    assert not ok and any("outside engagement" in r for r in reasons)
    # never silently re-scoped: both failures name the requested ids
    assert "DEPENDENCY_ANALYSIS" in reasons[0] or "tgt_ghost" in reasons[0]


def test_lifecycle_transitions_are_deterministic_and_fail_closed() -> None:
    draft = engagement(status=EngagementStatus.DRAFT)
    active = transition_engagement(draft, EngagementStatus.ACTIVE)
    assert active.status is EngagementStatus.ACTIVE
    suspended = transition_engagement(active, EngagementStatus.SUSPENDED)
    reactivated = transition_engagement(suspended, EngagementStatus.ACTIVE)
    assert reactivated.status is EngagementStatus.ACTIVE
    revoked = transition_engagement(active, EngagementStatus.REVOKED)
    assert revoked.status is EngagementStatus.REVOKED
    # revoked/completed are terminal: any further transition fails closed
    for target in EngagementStatus:
        with pytest.raises(ValueError):
            transition_engagement(revoked, target)
    completed = transition_engagement(active, EngagementStatus.COMPLETED)
    with pytest.raises(ValueError):
        transition_engagement(completed, EngagementStatus.ACTIVE)
    # illegal jumps fail closed
    with pytest.raises(ValueError):
        transition_engagement(draft, EngagementStatus.COMPLETED)


def test_taxonomy_is_closed_and_risk_ordered() -> None:
    assert set(SECURITY_CAPABILITIES) == {
        "RECON",
        "ANALYSIS",
        "CONFIGURATION_REVIEW",
        "CODE_ANALYSIS",
        "DEPENDENCY_ANALYSIS",
        "VULNERABILITY_VALIDATION",
        "EVIDENCE_COLLECTION",
    }
    validation = SECURITY_CAPABILITIES["VULNERABILITY_VALIDATION"]
    assert validation.risk.rank == 2 and validation.sandbox_class is SandboxClass.ISOLATED
    recon = SECURITY_CAPABILITIES["RECON"]
    assert recon.risk.rank < validation.risk.rank
    # taxonomy entries are closed contracts
    with pytest.raises(ValidationError):
        SecurityCapabilitySpec.model_validate(
            {**recon.model_dump(mode="json"), "exploit_generator": True}
        )
    with pytest.raises(ValidationError):
        SecurityCapabilitySpec.model_validate(
            {**recon.model_dump(mode="json"), "kind": "CREDENTIAL_THEFT"}
        )
