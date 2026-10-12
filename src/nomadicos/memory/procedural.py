"""Procedural memory — durable, RETRIEVAL-ONLY procedures (SPEC §32, §52E-G; Phase 11D).

A procedure describes what worked in a previously VERIFIED episode:
objective/context + concise step pattern + verification provenance. It is
descriptive data — never an executable instruction, never authority:

- a stored procedure cannot authorize anything, cannot mint an
  execution artifact, and is never run by memory; a worker may only cite
  it in a normal Action-IR proposal that travels the standard chain
  (engine text -> Action IR -> validator -> authority -> executor);
- trusted (verified) procedures require a real goal-verifier PASS
  artifact; model claims, critic suggestions, bare executor success, and
  unverified outcomes are rejected;
- unverified procedures are stored only as EXPLICITLY unverified data;
- identity is deterministic (objective + step pattern + source task);
  exact duplicates resolve to one record; genuinely distinct procedures
  are preserved side by side;
- never stored: full prompts, hidden reasoning, chain-of-thought, raw
  transcripts, tool-output blobs, credentials, or authority grants;
- persistence uses ONLY the existing MemoryStore; no authority/executor/
  routing imports, no SUCCESS write path, no execution of any kind.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord
from nomadicos.contracts.verification import VerificationOutcome, VerificationResult
from nomadicos.memory.store import MemoryStore

_LEGACY_VERIFIER = "legacy-placeholder"

#: retrieval tags; provenance.verified is the authoritative flag
TAG_VERIFIED_PROCEDURE = "procedure:verified"
TAG_UNVERIFIED_PROCEDURE = "procedure:unverified"

_OBJECTIVE_CHARS = 120
_STEP_CHARS = 120
_MAX_STEPS = 6
_STEP_CHARS = 120
_CONTENT_CHARS = 600


def _verifier_passed(goal_verdict: VerificationResult | None) -> bool:
    """Real GoalVerifier PASS (SPEC §8.9-§8.12) — the only trust source."""
    return (
        goal_verdict is not None
        and goal_verdict.verifier not in ("", _LEGACY_VERIFIER)
        and goal_verdict.verdict is VerificationOutcome.PASS
    )


def _norm_steps(steps) -> tuple[str, ...]:
    return tuple(
        " ".join(s.split())[:_STEP_CHARS] for s in steps if s and s.strip()
    )[:_MAX_STEPS]


def procedure_id(task_id: str, objective: str, steps) -> str:
    """Deterministic procedure identity: objective + step pattern + task."""
    norm = "|".join(s.lower() for s in _norm_steps(steps))
    digest = hashlib.sha256(
        f"{task_id}|{' '.join(objective.split()).lower()}|{norm}".encode()
    ).hexdigest()[:16]
    return f"mem-proc-{digest}"


def _procedure_record(
    *,
    task_id: str,
    objective: str,
    steps: tuple[str, ...],
    verified: bool,
    goal_verdict,
    source_event_id: str | None,
) -> MemoryRecord:
    content = (
        f"PROCEDURE for {objective.strip()[:_OBJECTIVE_CHARS]!r} steps: "
        + " -> ".join(steps)
        + f" (observed outcome: tests passed, verified={verified})"
        + (f" evidence=verif:{goal_verdict.id}" if goal_verdict is not None else "")
    )[:_CONTENT_CHARS]
    return MemoryRecord(
        id=procedure_id(task_id, objective, steps),
        kind=MemoryKind.PROCEDURAL,
        content=content,
        tags=[
            "procedure",
            TAG_VERIFIED_PROCEDURE if verified else TAG_UNVERIFIED_PROCEDURE,
            f"task:{task_id[:48]}",
        ],
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


def record_procedure(
    store: MemoryStore,
    *,
    task_id: str,
    objective: str,
    steps: Sequence[str],
    source_event_id: str | None = None,
) -> MemoryRecord | None:
    """Store an EXPLICITLY UNVERIFIED procedure (descriptive data only)."""
    steps_n = _norm_steps(steps)
    if not steps_n:
        return None
    record = _procedure_record(
        task_id=task_id,
        objective=objective,
        steps=steps_n,
        verified=False,
        goal_verdict=None,
        source_event_id=source_event_id,
    )
    return _write(store, record)


def record_verified_procedure(
    store: MemoryStore,
    *,
    task_id: str,
    objective: str,
    steps: Sequence[str],
    goal_verdict: VerificationResult,
    source_event_id: str | None = None,
) -> MemoryRecord | None:
    """Store a TRUSTED procedure; requires a real goal-verifier PASS
    artifact. Claims, critic suggestions, and bare executor success are
    rejected (returns None)."""
    if not _verifier_passed(goal_verdict):
        return None
    steps_n = _norm_steps(steps)
    if not steps_n:
        return None
    record = _procedure_record(
        task_id=task_id,
        objective=objective,
        steps=steps_n,
        verified=True,
        goal_verdict=goal_verdict,
        source_event_id=source_event_id,
    )
    return _write(store, record)
