"""Security audit trail (Sprint 9): append-only traces of what ACTUALLY
happened at every gate.

Invariants:
- PROPOSED is never EXECUTED: `executed=True` requires a real execution
  result reference (the contract validator enforces this); the Brain can
  never fabricate successful execution through the audit layer;
- append-only with a deterministic per-trail sequence;
- bounded: a full trail FAILS CLOSED on further appends (audit records
  are never silently evicted);
- every record is scrubbed before persistence-shaped storage.
"""

from __future__ import annotations

import hashlib

from nomadicos.contracts.security import AuditRecord, SecurityAction
from nomadicos.memory.scrub import scrub

MAX_AUDIT_RECORDS = 512


def audit_id(engagement_id: str, target_id: str, capability: str, sequence: int) -> str:
    """Deterministic audit identity (replayable)."""
    digest = hashlib.sha256(
        f"{engagement_id}|{target_id}|{capability}|{sequence}".encode()
    ).hexdigest()[:16]
    return f"audit-{digest}"


class AuditTrail:
    """One engagement's append-only audit trail."""

    def __init__(self, *, max_records: int = MAX_AUDIT_RECORDS) -> None:
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        self._max = max_records
        self._records: list[AuditRecord] = []

    # ------------------------------------------------------------- append ---
    def record(self, record: AuditRecord) -> AuditRecord:
        if len(self._records) >= self._max:
            raise OverflowError(f"audit trail full ({self._max}); refusing to drop audit records")
        sequence = len(self._records)
        stamped = record.model_copy(
            update={
                "sequence": sequence,
                "audit_id": audit_id(
                    record.engagement_id, record.target_id, record.capability, sequence
                ),
            }
        )
        self._records.append(stamped)
        return stamped

    # -------------------------------------------------------------- gates ---
    def record_proposed(self, action: SecurityAction) -> AuditRecord:
        """A PROPOSAL was made — never an execution."""
        return self.record(
            AuditRecord(
                audit_id="",
                sequence=0,
                engagement_id=action.engagement_id,
                target_id=action.target_id,
                sandbox_id=action.sandbox_id,
                capability=action.capability.value,
                invocation_id=action.invocation_id,
                proposed=True,
            )
        )

    def record_validation(self, action: SecurityAction, ok: bool, reason: str = "") -> AuditRecord:
        return self.record(
            AuditRecord(
                audit_id="",
                sequence=0,
                engagement_id=action.engagement_id,
                target_id=action.target_id,
                capability=action.capability.value,
                invocation_id=action.invocation_id,
                proposed=False,
                validated=ok,
                failure=scrub(reason)[:200],
            )
        )

    def record_authorization(
        self, action: SecurityAction, ok: bool, reason: str = ""
    ) -> AuditRecord:
        return self.record(
            AuditRecord(
                audit_id="",
                sequence=0,
                engagement_id=action.engagement_id,
                target_id=action.target_id,
                capability=action.capability.value,
                invocation_id=action.invocation_id,
                proposed=False,
                authorized=ok,
                failure=scrub(reason)[:200],
            )
        )

    def record_execution(self, action: SecurityAction, execution_ref: str) -> AuditRecord:
        """Execution is recorded ONLY from a real result reference —
        passing a proposal here cannot fabricate an execution."""
        return self.record(
            AuditRecord(
                audit_id="",
                sequence=0,
                engagement_id=action.engagement_id,
                target_id=action.target_id,
                sandbox_id=action.sandbox_id,
                capability=action.capability.value,
                invocation_id=action.invocation_id,
                executed=True,
                execution_ref=execution_ref,
            )
        )

    def record_failure(
        self,
        engagement_id: str,
        target_id: str,
        capability: str,
        invocation_id: str,
        reason: str,
    ) -> AuditRecord:
        return self.record(
            AuditRecord(
                audit_id="",
                sequence=0,
                engagement_id=engagement_id,
                target_id=target_id,
                capability=capability,
                invocation_id=invocation_id,
                failure=scrub(reason)[:200],
            )
        )

    # --------------------------------------------------------------- read ---
    def entries(self, engagement_id: str | None = None) -> list[AuditRecord]:
        """Deterministic (sequence) order; optional engagement filter."""
        return [
            record
            for record in self._records
            if engagement_id is None or record.engagement_id == engagement_id
        ]

    def __len__(self) -> int:
        return len(self._records)
