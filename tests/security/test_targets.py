"""Sprint 9 — target registry + sandbox: explicit registration, bounded
ids, deterministic binding, and fail-closed enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.security import (
    Engagement,
    EngagementStatus,
    SandboxClass,
    SandboxPolicy,
    SecurityCapabilityKind,
    TargetRecord,
    TargetType,
)
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.security.sandbox import SandboxManager
from nomadicos.security.targets import TargetRegistry


def engagement(**overrides) -> Engagement:
    base = dict(
        engagement_id="eng_lab",
        owner_reference="owner",
        purpose="audit the sample repository",
        allowed_targets=["tgt_repo"],
        allowed_capabilities=["RECON", "CODE_ANALYSIS"],
        sandbox_policy=SandboxPolicy(),
        status=EngagementStatus.ACTIVE,
    )
    base.update(overrides)
    return Engagement(**base)


def target(**overrides) -> TargetRecord:
    base = dict(
        target_id="tgt_repo",
        type=TargetType.REPOSITORY,
        canonical_identifier="repo://sample",
        allowed_operations=["filesystem.read", "filesystem.exists"],
        environment="local",
        enabled=True,
        engagement_id="",
    )
    base.update(overrides)
    return TargetRecord(**base)


# --------------------------------------------------------------- targets ---


def test_target_registration_lookup_and_duplicates() -> None:
    registry = TargetRegistry()
    registry.register(target())
    found = registry.lookup("tgt_repo")
    assert found is not None and found.type is TargetType.REPOSITORY
    assert registry.lookup("ghost") is None  # unknown fails closed
    with pytest.raises(InvalidProposal):
        registry.register(target())  # duplicates rejected, never overwritten


def test_target_contracts_bounded_and_closed() -> None:
    with pytest.raises(ValidationError):
        TargetRecord.model_validate({**target().model_dump(mode="json"), "discovery": True})
    with pytest.raises(ValidationError):
        target(target_id="x" * 65)
    with pytest.raises(ValidationError):
        target(canonical_identifier="")
    with pytest.raises(ValidationError):
        target(canonical_identifier="repo:// password=hunter2")
    with pytest.raises(ValidationError):
        target(allowed_operations=["not-a-capability"])


def test_target_matching_deterministic_and_fail_closed() -> None:
    registry = TargetRegistry()
    registry.register(target())
    # unknown target
    found, reason = registry.match("tgt_ghost", "eng_lab")
    assert found is None and "unknown target" in reason
    # disabled target
    registry.register(target(target_id="tgt_off", enabled=False))
    found, reason = registry.match("tgt_off", "eng_lab")
    assert found is None and "disabled" in reason
    # engagement-bound target accessed under another engagement
    registry.register(target(target_id="tgt_bound", engagement_id="eng_other"))
    found, reason = registry.match("tgt_bound", "eng_lab")
    assert found is None and "bound to engagement" in reason
    # in-scope match succeeds deterministically
    found, reason = registry.match("tgt_repo", "eng_lab")
    assert found is not None and reason == ""


def test_target_registry_bounded() -> None:
    registry = TargetRegistry(max_targets=1)
    registry.register(target())
    with pytest.raises(InvalidProposal):
        registry.register(target(target_id="tgt_two"))


# -------------------------------------------------------------- sandboxes ---


def test_sandbox_creation_binds_engagement_and_target() -> None:
    manager = SandboxManager()
    sandbox = manager.create("sb_lab", engagement(), target(), sandbox_class=SandboxClass.WORKSPACE)
    assert sandbox.engagement_id == "eng_lab"
    assert sandbox.target_id == "tgt_repo"
    assert sandbox.allowed_capabilities == ["RECON", "CODE_ANALYSIS"]  # engagement grant
    assert sandbox.active is True
    with pytest.raises(InvalidProposal):
        manager.create("sb_lab", engagement(), target(), sandbox_class=SandboxClass.WORKSPACE)


def test_sandbox_never_exceeds_engagement_grant() -> None:
    manager = SandboxManager()
    with pytest.raises(InvalidProposal):
        manager.create(
            "sb_over",
            engagement(),
            target(),
            sandbox_class=SandboxClass.WORKSPACE,
            capabilities=["DEPENDENCY_ANALYSIS"],  # NOT granted by the engagement
        )
    with pytest.raises(InvalidProposal):
        manager.create(
            "sb_none", engagement(), target(), sandbox_class=SandboxClass.WORKSPACE, capabilities=[]
        )


def test_sandbox_validation_fail_closed() -> None:
    manager = SandboxManager()
    manager.create("sb_lab", engagement(), target(), sandbox_class=SandboxClass.WORKSPACE)
    ok, reasons = manager.validate("sb_lab", engagement(), target(), SecurityCapabilityKind.RECON)
    assert ok and reasons == []
    # unknown sandbox
    ok, reasons = manager.validate("sb_ghost", engagement(), target(), SecurityCapabilityKind.RECON)
    assert not ok and "unknown sandbox" in reasons[0]
    # engagement mismatch
    other = engagement(engagement_id="eng_other")
    ok, reasons = manager.validate("sb_lab", other, target(), SecurityCapabilityKind.RECON)
    assert not ok and any("bound to engagement" in r for r in reasons)
    # target mismatch
    other_target = target(target_id="tgt_other")
    ok, reasons = manager.validate(
        "sb_lab", engagement(), other_target, SecurityCapabilityKind.RECON
    )
    assert not ok and any("bound to target" in r for r in reasons)
    # capability not permitted by THIS sandbox
    manager2 = SandboxManager()
    manager2.create(
        "sb_narrow",
        engagement(),
        target(),
        sandbox_class=SandboxClass.WORKSPACE,
        capabilities=["RECON"],
    )
    ok, reasons = manager2.validate(
        "sb_narrow", engagement(), target(), SecurityCapabilityKind.CODE_ANALYSIS
    )
    assert not ok and any("not permitted by sandbox" in r for r in reasons)


def test_isolation_class_enforced_per_capability() -> None:
    """VULNERABILITY_VALIDATION requires ISOLATED — a WORKSPACE sandbox
    fails closed."""
    manager = SandboxManager()
    full_engagement = engagement(allowed_capabilities=["RECON", "VULNERABILITY_VALIDATION"])
    manager.create("sb_ws", full_engagement, target(), sandbox_class=SandboxClass.WORKSPACE)
    ok, reasons = manager.validate(
        "sb_ws", full_engagement, target(), SecurityCapabilityKind.VULNERABILITY_VALIDATION
    )
    assert not ok
    assert any("requires sandbox class ISOLATED" in r for r in reasons)
    manager.create("sb_iso", full_engagement, target(), sandbox_class=SandboxClass.ISOLATED)
    ok, reasons = manager.validate(
        "sb_iso", full_engagement, target(), SecurityCapabilityKind.VULNERABILITY_VALIDATION
    )
    assert ok and reasons == []


def test_sandbox_deactivation_and_policy_bounds() -> None:
    manager = SandboxManager()
    manager.create("sb_lab", engagement(), target(), sandbox_class=SandboxClass.WORKSPACE)
    assert manager.deactivate("sb_lab") is True
    ok, reasons = manager.validate("sb_lab", engagement(), target(), SecurityCapabilityKind.RECON)
    assert not ok and any("inactive" in r for r in reasons)
    assert manager.deactivate("sb_lab") is False  # already inactive
    assert manager.deactivate("sb_ghost") is False  # unknown: fail closed
    with pytest.raises(ValidationError):
        SandboxPolicy(max_cpu_seconds=10_000_001)
