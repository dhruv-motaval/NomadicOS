"""Sprint 9 — evidence store + audit trail: append-only, bounded,
scrubbed, provenance-preserving, proposed ≠ executed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.security import (
    AuditRecord,
    EvidenceType,
    SecurityAction,
    SecurityCapabilityKind,
    SecurityEvidence,
)
from nomadicos.security.audit import AuditTrail
from nomadicos.security.evidence import EvidenceStore


def action(**overrides) -> SecurityAction:
    base = dict(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        sandbox_id="sb_lab",
        capability=SecurityCapabilityKind.RECON,
        args={"path": "src"},
        expected_evidence=EvidenceType.OBSERVATION,
    )
    base.update(overrides)
    return SecurityAction(**base)


# --------------------------------------------------------------- evidence ---


def test_evidence_append_is_deterministic_and_bound() -> None:
    store = EvidenceStore()
    first = store.append(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
        content="found config",
        provenance=["exec_1"],
        invocation_id="task_1",
    )
    second = store.append(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
        content="found config",
        provenance=["exec_1"],
        invocation_id="task_1",
    )
    assert first.sequence == 0 and second.sequence == 1  # deterministic order
    assert first.evidence_id != second.evidence_id
    assert first.evidence_id.startswith("evt-sec-")
    assert first.engagement_id == "eng_lab" and first.target_id == "tgt_repo"
    assert first.provenance == ["exec_1"]  # provenance preserved
    assert len(store) == 2


def test_evidence_content_scrubbed_before_persistence() -> None:
    store = EvidenceStore()
    entry = store.append(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        capability="RECON",
        classification=EvidenceType.FINDING,
        content="response contained password=hunter2 in the fixture",
        source="token: abc123 probe",
    )
    assert "hunter2" not in entry.content
    assert "[REDACTED]" in entry.content
    assert "abc123" not in entry.source


def test_evidence_rejects_unscrubbed_secrets_at_contract_level() -> None:
    with pytest.raises(ValidationError):
        SecurityEvidence(
            evidence_id="evt-sec-x",
            engagement_id="e",
            target_id="t",
            capability="RECON",
            sequence=0,
            classification=EvidenceType.FINDING,
            content="api_key: supersecretvalue",
        )


def test_evidence_store_is_append_only_and_bounded() -> None:
    store = EvidenceStore(max_entries=2)
    store.append(
        engagement_id="e",
        target_id="t",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
    )
    store.append(
        engagement_id="e",
        target_id="t",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
    )
    with pytest.raises(OverflowError):
        # full store FAILS CLOSED — audit evidence is never silently evicted
        store.append(
            engagement_id="e",
            target_id="t",
            capability="RECON",
            classification=EvidenceType.OBSERVATION,
        )
    assert not hasattr(store, "delete")
    assert not hasattr(store, "update")


def test_evidence_persists_and_reloads(tmp_path) -> None:
    path = tmp_path / "evidence.jsonl"
    store = EvidenceStore(path)
    entry = store.append(
        engagement_id="eng_lab",
        target_id="tgt_repo",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
        content="observed layout",
        provenance=["exec_9"],
        invocation_id="task_7",
    )
    reloaded = EvidenceStore(path)
    assert len(reloaded) == 1
    assert reloaded.entries()[0].model_dump(mode="json") == entry.model_dump(mode="json")
    # engagement filter deterministic
    assert reloaded.entries("eng_lab") and reloaded.entries("eng_other") == []


def test_evidence_survives_corrupt_lines(tmp_path) -> None:
    path = tmp_path / "evidence.jsonl"
    store = EvidenceStore(path)
    store.append(
        engagement_id="e",
        target_id="t",
        capability="RECON",
        classification=EvidenceType.OBSERVATION,
    )
    path.write_text("{corrupt\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    reloaded = EvidenceStore(path)
    assert len(reloaded) == 1  # corrupt line dropped, good evidence kept


# ------------------------------------------------------------------ audit ---


def test_audit_distinguishes_proposed_from_executed() -> None:
    trail = AuditTrail()
    act = action()
    trail.record_proposed(act)  # a PROPOSAL only
    entries = trail.entries()
    assert entries[0].proposed is True
    assert entries[0].executed is None  # never fabricated as executed
    # an execution record REQUIRES a real execution result reference
    with pytest.raises(ValidationError):
        AuditRecord(
            audit_id="",
            sequence=0,
            engagement_id="eng_lab",
            target_id="tgt_repo",
            capability="RECON",
            executed=True,
            execution_ref="",
        )
    trail.record_execution(act, "exec_abc123")
    executed_entry = trail.entries()[-1]
    assert executed_entry.executed is True and executed_entry.execution_ref == "exec_abc123"
    assert executed_entry.proposed is False


def test_audit_records_every_gate() -> None:
    trail = AuditTrail()
    act = action()
    trail.record_proposed(act)
    trail.record_validation(act, ok=True)
    trail.record_authorization(act, ok=True)
    trail.record_execution(act, "exec_1")
    gates = [(e.proposed, e.validated, e.authorized, e.executed) for e in trail.entries()]
    assert gates == [
        (True, None, None, None),
        (False, True, None, None),
        (False, None, True, None),
        (False, None, None, True),
    ]
    # deterministic sequence
    assert [e.sequence for e in trail.entries()] == [0, 1, 2, 3]


def test_audit_records_failures_and_scrubs_them() -> None:
    trail = AuditTrail()
    trail.record_failure(
        "eng_lab",
        "tgt_repo",
        "RECON",
        "task_1",
        "authorization denied: token=abc123 leaked in reason",
    )
    entry = trail.entries()[0]
    assert entry.failure and "abc123" not in entry.failure
    assert "[REDACTED]" in entry.failure


def test_audit_trail_is_append_only_and_bounded() -> None:
    trail = AuditTrail(max_records=2)
    act = action()
    trail.record_proposed(act)
    trail.record_validation(act, ok=False, reason="scope")
    with pytest.raises(OverflowError):
        trail.record_authorization(act, ok=True)  # full trail fails closed
    assert len(trail) == 2
