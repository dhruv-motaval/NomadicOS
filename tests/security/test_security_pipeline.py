"""Sprint 9 — full security pipeline: engagement → target → capability →
sandbox → Action IR (REAL ProposalValidator) → authorization (REAL
AuthorizationService) → evidence → audit; plus the enforcement benchmark
suite. The existing runtime remains the ONLY authority."""

from __future__ import annotations

import pytest

from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.policy import CapabilityPolicy
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.action import ActionProposal, AuthorizedAction
from nomadicos.contracts.security import (
    EvidenceType,
    SandboxClass,
    SecurityAction,
    SecurityCapabilityKind,
    TargetRecord,
    TargetType,
)
from nomadicos.kernel.events import EventLogger
from nomadicos.security.adapter import to_action_proposal
from nomadicos.security.audit import AuditTrail
from nomadicos.security.benchmarks import (
    active_fixture_engagement,
    fixture_evidence,
    run_security_benchmarks,
)
from nomadicos.security.engagement import validate_engagement
from nomadicos.security.evidence import EvidenceStore
from nomadicos.security.sandbox import SandboxManager
from nomadicos.security.targets import TargetRegistry
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.filesystem import FilesystemTool


def engagement():
    from nomadicos.contracts.security import Engagement, EngagementStatus, SandboxPolicy

    return Engagement(
        engagement_id="eng_lab",
        owner_reference="owner",
        purpose="audit the sample repository",
        allowed_targets=["tgt_repo"],
        allowed_capabilities=["RECON", "CODE_ANALYSIS", "EVIDENCE_COLLECTION"],
        sandbox_policy=SandboxPolicy(),
        status=EngagementStatus.ACTIVE,
    )


def target():
    return TargetRecord(
        target_id="tgt_repo",
        type=TargetType.REPOSITORY,
        canonical_identifier="repo://sample",
        allowed_operations=["filesystem.read", "filesystem.exists"],
        environment="local",
    )


def security_action(**overrides):
    base = dict(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        sandbox_id="sb_lab",
        capability=SecurityCapabilityKind.RECON,
        args={"path": "src/nomadicos"},
        expected_evidence=EvidenceType.OBSERVATION,
    )
    base.update(overrides)
    return SecurityAction(**base)


def test_full_security_pipeline_through_real_gates(tmp_path) -> None:
    """SecurityAction → ActionProposal → REAL ProposalValidator → REAL
    AuthorizationService → evidence + audit. The lane adds NO authority."""
    eng = engagement()
    tgt = target()
    targets = TargetRegistry()
    targets.register(tgt)
    sandboxes = SandboxManager()
    sandboxes.create("sb_lab", eng, tgt, sandbox_class=SandboxClass.WORKSPACE)
    action = security_action()
    audit = AuditTrail()

    ok, reasons = validate_engagement(eng, action.capability, action.target_id)
    assert ok, reasons
    audit.record_proposed(action)
    found, reason = targets.match(action.target_id, eng.engagement_id)
    assert found is not None
    ok, reasons = sandboxes.validate(action.sandbox_id, eng, found, action.capability)
    assert ok, reasons
    audit.record_validation(action, ok=True)

    proposal = to_action_proposal(
        action,
        eng,
        found,
        sandboxes.lookup("sb_lab"),
        model_id="model_1",
        task_id="task_sec",
        step_id="step_sec",
    )
    assert isinstance(proposal, ActionProposal)
    assert (proposal.tool, proposal.operation) == ("filesystem", "exists")

    # the REAL Action-IR validator is authoritative
    catalog = ToolRegistry()
    catalog.register(FilesystemTool())
    validator = ProposalValidator(catalog, EventLogger())
    capability_ref = validator.validate(
        proposal,
        ValidationContext(task_id="task_sec", step_id="step_sec", model_id="model_1"),
    )
    assert capability_ref.capability == "filesystem.exists"

    # the REAL AuthorizationService is authoritative (not the lane)
    store = AuthorityStore(tmp_path / "authority.json")
    policy = CapabilityPolicy(store, granted_patterns=["filesystem.*"])
    authz = AuthorizationService(store, policy, EventLogger())
    store.grant_full_pc_autonomy(source="test-owner")
    authorized = authz.authorize(proposal, capability_ref)
    assert isinstance(authorized, AuthorizedAction)
    audit.record_authorization(action, ok=True)

    # evidence + audit bind the outcome; execution itself stays with the
    # existing executor boundary (unchanged by Sprint 9)
    evidence_store = EvidenceStore()
    entry = evidence_store.append(
        engagement_id=eng.engagement_id,
        target_id=tgt.target_id,
        capability=action.capability.value,
        classification=EvidenceType.OBSERVATION,
        content="src tree observed",
        provenance=[proposal.id],
        invocation_id=action.invocation_id,
    )
    audit.record_execution(action, entry.evidence_id)
    kinds = [(e.proposed, e.validated, e.authorized, e.executed) for e in audit.entries()]
    assert kinds == [
        (True, None, None, None),
        (False, True, None, None),
        (False, None, True, None),
        (False, None, None, True),
    ]


def test_pipeline_rejects_out_of_scope_capability() -> None:
    eng = engagement()
    targets = TargetRegistry()
    targets.register(target())
    action = security_action(capability=SecurityCapabilityKind.DEPENDENCY_ANALYSIS)
    audit = AuditTrail()
    ok, reasons = validate_engagement(eng, action.capability, action.target_id)
    assert not ok  # capability not granted: chain stops here, fail closed
    audit.record_failure(
        eng.engagement_id,
        action.target_id,
        action.capability.value,
        action.invocation_id,
        reasons[0],
    )
    assert audit.entries()[-1].failure.startswith("capability")


def test_security_action_never_carries_authority() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SecurityAction.model_validate(
            {**security_action().model_dump(mode="json"), "authorized": True}
        )
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        security_action(args={"bypass_policy": True})
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        security_action(args={"command": "rm -rf /; token=abc123"})


def test_adapter_rejects_binding_substitution() -> None:
    eng = engagement()
    tgt = target()
    sandboxes = SandboxManager()
    sandboxes.create("sb_lab", eng, tgt, sandbox_class=SandboxClass.WORKSPACE)
    # swapped target: fail closed, no silent substitution
    with pytest.raises(ValueError):
        to_action_proposal(
            security_action(),
            eng,
            TargetRecord(
                target_id="tgt_other",
                type=TargetType.REPOSITORY,
                canonical_identifier="repo://other",
                allowed_operations=["filesystem.exists"],
            ),
            sandboxes.lookup("sb_lab"),
            model_id="m",
            task_id="t",
            step_id="s",
        )


def test_benchmark_suite_is_deterministic_and_enforcement_focused() -> None:
    def fixtures():
        return active_fixture_engagement(), fixture_evidence()

    first = run_security_benchmarks("sec-v1", fixtures, cases=2)
    second = run_security_benchmarks("sec-v1", fixtures, cases=2)
    assert [r.passed for r in first] == [r.passed for r in second]  # deterministic
    assert len(first) == 12  # 2 cases x 6 gates, bounded
    gates = {r.gate for r in first}
    assert gates == {"scope", "authorization", "capability", "sandbox", "evidence", "audit"}
    assert all(r.passed for r in first)  # active fixture enforces everything


def test_benchmark_detects_violations() -> None:
    from nomadicos.contracts.security import EngagementStatus

    def fixtures():
        revoked = active_fixture_engagement().model_copy(
            update={"status": EngagementStatus.REVOKED}
        )
        return revoked, fixture_evidence()

    records = run_security_benchmarks("sec-v1", fixtures, cases=1)
    authorization = next(r for r in records if r.gate == "authorization")
    assert authorization.passed is False  # honest: revoked engagement violates
    assert "VIOLATION" in authorization.detail
