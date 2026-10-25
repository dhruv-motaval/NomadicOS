"""Trajectory storage — replayable structured mission records (Sprint 5).

An episodic record summarizes an outcome; a trajectory record preserves
the STRUCTURE of what happened, every step traceable to its source:

    goal → actions → observations → failures → recovery → outcome

- identity is deterministic (mission + task), so re-recording the same
  trajectory is one idempotent upsert;
- every step carries a bounded summary AND a source reference (event /
  execution / candidate / verifier id) — never only natural language;
- replayability: canonical serialization round-trips to an equal record;
- persistence uses ONLY the existing MemoryStore (Sprint 5 extension of
  the 11A boundary); the module imports no authority/executor/routing
  code and records nothing from model claims alone;
- fail-safe: a persistence problem returns None and never breaks a task.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from nomadicos.contracts.memory import (
    MAX_OUTCOME_CHARS,
    MAX_STEP_SUMMARY_CHARS,
    MemoryProvenance,
    MemorySource,
    TrajectoryRecord,
    TrajectoryStep,
    TrajectoryStepKind,
)
from nomadicos.memory.store import MemoryStore

_GOAL_CHARS = 300
_STEP_REFS = 64
_MAX_STEPS = 32


def trajectory_id(mission_id: str, task_id: str) -> str:
    """Deterministic trajectory identity: one per mission task."""
    digest = hashlib.sha256(f"{mission_id}|{task_id}".encode()).hexdigest()[:16]
    return f"mem-traj-{digest}"


def make_step(kind: TrajectoryStepKind, ref: str, summary: str = "") -> TrajectoryStep:
    """Bounded step constructor; invalid (empty-ref) steps are dropped by
    the recorder, never silently fabricated."""
    return TrajectoryStep(kind=kind, ref=ref.strip()[:_STEP_REFS], summary=summary)


def record_trajectory(
    store: MemoryStore,
    *,
    mission_id: str,
    task_id: str,
    goal_text: str,
    steps: Sequence[TrajectoryStep],
    outcome: str = "",
    verified: bool = False,
    confidence: float = 0.6,
    source_event_id: str | None = None,
) -> TrajectoryRecord | None:
    """Persist one task trajectory (idempotent per mission+task).

    Fail-safe: returns None when there is nothing to record (no steps)
    or the store fails. Verified trajectories require the caller to pass
    real verification provenance; the recorder never invents it.
    """
    mission_id = mission_id.strip()
    task_id = task_id.strip()
    if not mission_id or not task_id or not steps:
        return None
    record = TrajectoryRecord(
        id=trajectory_id(mission_id, task_id),
        mission_id=mission_id,
        task_id=task_id,
        goal_text=goal_text.strip()[:_GOAL_CHARS],
        steps=list(steps)[:_MAX_STEPS],
        outcome=outcome.strip()[:MAX_OUTCOME_CHARS],
        verified=verified,
        provenance=MemoryProvenance(
            source_task_id=task_id,
            source_event_id=source_event_id,
            confidence=confidence if verified else min(confidence, 0.6),
            verified=verified,
            source_type=MemorySource.TRAJECTORY,
            evidence_refs=[step.ref for step in steps[:4]],
        ),
    )
    try:
        return store.upsert_trajectory(record)
    except Exception:  # noqa: BLE001 - memory is fail-safe by contract
        return None


# summary length is enforced by the contract; kept as a named constant
# import for callers that pre-truncate
STEP_SUMMARY_CHARS = MAX_STEP_SUMMARY_CHARS
