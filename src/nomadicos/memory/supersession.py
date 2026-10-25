"""Supersession — explicit memory replacement that preserves history
(SPEC §32, §52G; Sprint 5).

New evidence replaces stale knowledge WITHOUT deleting it:

    old fact  →  new evidence  →  new record  →  old marked superseded

- the old record is KEPT with its full provenance (historical audit);
  only its supersession status flips (``superseded=True``,
  ``superseded_by=<new id>``);
- a ``supersedes`` graph relation (new → old) links the two records in
  the object graph, carrying a structured ``SupersessionRecord``;
- retrieval prefers non-superseded records (deterministic penalty in
  the retriever) while superseded records remain retrievable data;
- fail-safe: any store problem returns None and never breaks a task;
- same-kind replacement only: a fact supersedes a fact, an episode an
  episode (cross-kind replacement is rejected as a programming error).
"""

from __future__ import annotations

from datetime import UTC, datetime

from nomadicos.contracts.memory import (
    MemoryProvenance,
    MemoryRecord,
    MemorySource,
    SupersessionRecord,
)
from nomadicos.memory.graph import relation_id
from nomadicos.memory.store import MemoryStore, RelationRecord

SUPERSEDES = "supersedes"
_REASON_CHARS = 200


def supersede(
    store: MemoryStore,
    *,
    old_id: str,
    new: MemoryRecord,
    reason: str = "",
    source_event_id: str | None = None,
) -> tuple[MemoryRecord, MemoryRecord] | None:
    """Mark ``old_id`` as superseded by ``new``; keep both records.

    Returns (stored_new, superseded_old), or None when the old record is
    missing, the replacement is invalid, or the store fails (fail-safe).
    """
    if old_id == new.id:
        return None
    old = next((r for r in store.records() if r.id == old_id), None)
    if old is None or old.kind is not new.kind:
        return None
    if new.superseded:
        return None
    try:
        stored_new = store.write(new)
        marked_old = old.model_copy(update={"superseded": True, "superseded_by": stored_new.id})
        stored_old = store.write(marked_old)
        relation = RelationRecord(
            id=relation_id(stored_new.id, SUPERSEDES, stored_old.id),
            source=stored_new.id,
            relation=SUPERSEDES,
            target=stored_old.id,
            properties={
                "supersession": SupersessionRecord(
                    superseded_id=stored_old.id,
                    superseded_by=stored_new.id,
                    reason=reason[:_REASON_CHARS],
                    at=datetime.now(UTC),
                ).model_dump(mode="json")
            },
            provenance=MemoryProvenance(
                source_task_id=new.provenance.source_task_id,
                source_event_id=source_event_id,
                confidence=new.provenance.confidence,
                verified=new.provenance.verified,
                source_type=MemorySource.TRAJECTORY,
            ),
        )
        store.upsert_relation(relation)
        return stored_new, stored_old
    except Exception:  # noqa: BLE001 - memory is fail-safe by contract
        return None
