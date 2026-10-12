"""Episodic memory — durable records of completed task episodes (§52G; Phase 11C).

An episodic record captures what ACTUALLY happened during a task, from
SYSTEM evidence only. Core invariant:

    proposed action != executed action != verified outcome

Classification rules (enforced structurally — the API accepts no model-
authored completion field, so a model claim / critic ACCEPT / bare executor
success can never become a verified-success episode):

- VERIFIED SUCCESS: only a real goal-verifier PASS artifact (a
  ``VerificationResult`` whose verifier id is a real verifier, never the
  legacy placeholder) PLUS the task lifecycle SUCCESS state;
- FAILED: verifier NOT_PASS/BLOCKED or lifecycle FAILED/BLOCKED;
- PARTIAL: actions executed but the goal was never independently verified
  (includes executor success, critic ACCEPT, and bare model claims);
- NOTHING recorded from claims alone: no executions and no verification
  artifact -> no episode at all.

Persistence uses ONLY the existing MemoryStore (11A boundary); provenance
carries source task/event ids, the honest verified flag, and bounded
confidence. A memory write failure is swallowed (record dropped, ``None``
returned) — memory persistence can never turn a successful task into a
failed one, and this module imports no authority/executor/routing code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord
from nomadicos.contracts.verification import VerificationOutcome, VerificationResult

_LEGACY_VERIFIER = "legacy-placeholder"

#: retrieval tags (used by 11F); provenance.verified is the authoritative flag
TAG_VERIFIED_SUCCESS = "episode:verified-success"
TAG_FAILED = "episode:failed"
TAG_PARTIAL = "episode:partial"

_CONTENT_LIMIT = 600
_OBJECTIVE_CHARS = 160
_MAX_REFS = 4


def episode_id(task_id: str, tag: str) -> str:
    """Deterministic episode identity: same task + same outcome class =>
    the same record id (store id-keyed upsert keeps exactly one record)."""
    digest = hashlib.sha256(f"{task_id}|{tag}".encode()).hexdigest()[:16]
    return f"mem-epi-{digest}"


def classify_episode(
    goal_verdict: VerificationResult | None,
    task_status: TaskStatus | None,
    executions: int,
) -> tuple[str, bool] | None:
    """(tag, verified) for the episode, or None when nothing observable
    happened: claims, proposals, authorizations, critic suggestions, and
    routing states alone never create an episode (SPEC §52G)."""
    verdict = goal_verdict.verdict if goal_verdict is not None else None
    real_verifier = goal_verdict is not None and goal_verdict.verifier not in (
        "",
        _LEGACY_VERIFIER,
    )
    if (
        real_verifier
        and verdict is VerificationOutcome.PASS
        and task_status is TaskStatus.SUCCESS
    ):
        return TAG_VERIFIED_SUCCESS, True
    if executions <= 0 and goal_verdict is None:
        return None  # model claim / critic ACCEPT / proposal only
    if goal_verdict is not None and verdict in (
        VerificationOutcome.NOT_PASS,
        VerificationOutcome.BLOCKED,
    ):
        return TAG_FAILED, False
    if task_status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
        return TAG_FAILED, False
    return TAG_PARTIAL, False  # ran, but never independently verified


def _episode_record(
    *,
    task_id: str,
    objective: str,
    tag: str,
    verified: bool,
    goal_verdict: VerificationResult | None,
    task_status: TaskStatus | None,
    executions: int,
    test_exits: Sequence[int],
    evidence_refs: Sequence[str],
    source_event_id: str | None,
) -> MemoryRecord:
    """Bounded record: identity, concise summary, outcome, evidence refs.
    Never stores prompts, model reasoning, raw transcripts, or arbitrary
    external content (SPEC §52G)."""
    refs = [f"verif:{goal_verdict.id}"] if goal_verdict is not None else []
    refs += [r[:64] for r in evidence_refs]
    refs = refs[:_MAX_REFS]
    content = (
        f"EPISODE {tag.removeprefix('episode:')} task={task_id[:48]} "
        f"goal={objective.strip()[:_OBJECTIVE_CHARS]!r} "
        f"status={task_status.value if task_status else 'unknown'} "
        f"goal_verdict="
        f"{goal_verdict.verdict.value if goal_verdict is not None else 'none'} "
        f"executions={executions} "
        f"test_exits={list(test_exits[:6])} refs={refs}"
    )[:_CONTENT_LIMIT]
    confidence = {TAG_VERIFIED_SUCCESS: 0.95, TAG_FAILED: 0.8}.get(tag, 0.6)
    return MemoryRecord(
        id=episode_id(task_id, tag),
        kind=MemoryKind.EPISODIC,
        content=content,
        tags=["episode", tag, f"task:{task_id[:48]}"],
        provenance=MemoryProvenance(
            source_task_id=task_id,
            source_event_id=source_event_id,
            confidence=confidence,
            verified=verified,
        ),
    )


def record_from_task_result(
    store: object,
    *,
    task_id: str,
    objective: str,
    goal_verdict: VerificationResult | None = None,
    task_status: TaskStatus | None = None,
    executions: int = 0,
    test_exits: Sequence[int] = (),
    evidence_refs: Sequence[str] = (),
    source_event_id: str | None = None,
) -> MemoryRecord | None:
    """Record one completed episode; returns the stored record, or None
    when there is no observable episode (claims only) or the write failed
    (memory persistence must never break the task — fail safe)."""
    classified = classify_episode(goal_verdict, task_status, executions)
    if classified is None:
        return None
    tag, verified = classified
    record = _episode_record(
        task_id=task_id,
        objective=objective,
        tag=tag,
        verified=verified,
        goal_verdict=goal_verdict,
        task_status=task_status,
        executions=executions,
        test_exits=test_exits,
        evidence_refs=evidence_refs,
        source_event_id=source_event_id,
    )
    try:
        return store.write(record)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - memory is fail-safe by contract
        return None
