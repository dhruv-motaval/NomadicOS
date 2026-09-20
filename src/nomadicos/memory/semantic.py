"""Semantic memory — durable factual knowledge (SPEC §32, §52E-G; Phase 11D).

Stable facts derived from ACTUAL verified execution outcomes. DATA only:

- trusted (verified) facts require a real goal-verifier PASS artifact —
  model claims, critic suggestions, bare executor success, plans,
  proposals, and unverified outcomes can never produce one;
- unverified facts are stored only as EXPLICITLY unverified data
  (provenance.verified=False, confidence capped by the contract);
- identity is deterministic and content-addressed per source task: the
  same fact from the same task resolves to one record; the same fact text
  from a DIFFERENT task is a distinct observation and is preserved;
- contradictions are append-only: conflicting facts never overwrite each
  other and there is no hidden latest-wins rule — both records survive
  with their own provenance (re-recording identical content is the only
  replace, an evidence upgrade of the same identity);
- content is bounded (no prompts, reasoning, transcripts, or secrets);
- persistence uses ONLY the existing MemoryStore; the module imports no
  authority/executor/routing code and has no SUCCESS write path.
"""

from __future__ import annotations

import hashlib

from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord
from nomadicos.contracts.verification import VerificationOutcome, VerificationResult
from nomadicos.memory.store import MemoryStore

_LEGACY_VERIFIER = "legacy-placeholder"

#: retrieval tags; provenance.verified is the authoritative flag
TAG_VERIFIED_FACT = "semantic:verified-fact"
TAG_UNVERIFIED_FACT = "semantic:fact-unverified"

_FACT_CHARS = 400


def _verifier_passed(goal_verdict: VerificationResult | None) -> bool:
    """Real GoalVerifier PASS (SPEC §8.9-§8.12) — the only trust source."""
    return (
        goal_verdict is not None
        and goal_verdict.verifier not in ("", _LEGACY_VERIFIER)
        and goal_verdict.verdict is VerificationOutcome.PASS
    )


def fact_id(fact: str, task_id: str) -> str:
    """Deterministic fact identity: normalized text + source task."""
    normalized = " ".join(fact.split()).lower()
    digest = hashlib.sha256(f"{task_id}|{normalized}".encode()).hexdigest()[:16]
    return f"mem-fact-{digest}"


def _fact_record(
    *,
    task_id: str,
    fact: str,
    verified: bool,
    goal_verdict: VerificationResult | None,
    source_event_id: str | None,
) -> MemoryRecord:
    text = " ".join(fact.split())[:_FACT_CHARS]
    content = f"FACT {fact_id(fact, task_id)}: {text}"[:_FACT_CHARS]
    return MemoryRecord(
        id=fact_id(fact, task_id),
        kind=MemoryKind.SEMANTIC,
        content=content,
        tags=["semantic", TAG_VERIFIED_FACT if verified else TAG_UNVERIFIED_FACT],
        provenance=MemoryProvenance(
            source_task_id=task_id,
            source_event_id=source_event_id,
            confidence=0.95 if verified else 0.6,
            verified=verified,
        ),
    )


def _write(store: MemoryStore, record: MemoryRecord) -> MemoryRecord | None:
    """Fail-safe persistence: a memory problem never breaks the task."""
    try:
        return store.write(record)
    except Exception:  # noqa: BLE001 - memory is fail-safe by contract
        return None


def record_fact(
    store: MemoryStore,
    *,
    task_id: str,
    fact: str,
    source_event_id: str | None = None,
) -> MemoryRecord | None:
    """Store an EXPLICITLY UNVERIFIED semantic fact (data, capped trust)."""
    fact = fact.strip()
    if not fact:
        return None
    record = _fact_record(
        task_id=task_id,
        fact=fact,
        verified=False,
        goal_verdict=None,
        source_event_id=source_event_id,
    )
    return _write(store, record)


def record_verified_fact(
    store: MemoryStore,
    *,
    task_id: str,
    fact: str,
    goal_verdict: VerificationResult,
    source_event_id: str | None = None,
) -> MemoryRecord | None:
    """Store a TRUSTED semantic fact; requires a real goal-verifier PASS
    artifact. Model claims, critic suggestions, bare executor success, and
    unverified outcomes are rejected (returns None)."""
    if not _verifier_passed(goal_verdict):
        return None
    record = _fact_record(
        task_id=task_id,
        fact=fact.strip(),
        verified=True,
        goal_verdict=goal_verdict,
        source_event_id=source_event_id,
    )
    return _write(store, record)
