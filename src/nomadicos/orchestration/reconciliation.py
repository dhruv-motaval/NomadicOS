"""Crash-safe recovery reconciliation (Phase 3, SPEC §56.14).

Consumes persisted workflow state + durable execution evidence to classify the
restart situation and return a typed recovery decision. Deterministic: never
consults model-generated prose to decide whether an external side effect
occurred.

Integrated into ``NomadicApp.resume_task`` (the primary LangGraph lifecycle).
Does not introduce a second execution loop and does not bypass Phase 2's
``classify_last_outcome()`` logic.

Key safety invariant: the executor consumes ``pending_authorized_id`` (via
``runtime.take()``) as its first action, and always either records an
``ExecutionResult`` or sets ``execution_missing=True``. So if neither an
execution result nor ``execution_missing`` is present, ``execute`` was never
called and no tool ran. The durable ledger is the final arbiter when state
alone cannot tell whether a side effect occurred.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nomadicos.contracts.core import Failure, TaskStatus
from nomadicos.contracts.execution import IntentStatus
from nomadicos.orchestration.runtime import TaskRuntime


class RecoveryDecision(StrEnum):
    """Deterministic recovery decisions across crash boundaries (§56.14)."""

    #: No in-flight action; resume from the last checkpointed plan step.
    RESUME_PLANNING = "RESUME_PLANNING"
    #: An authorization artifact was issued pre-restart but the in-memory
    #: issued table is lost → the artifact is unusable; revalidate and
    #: reauthorize under current permissions (never reuse the stale artifact).
    REAUTHORIZE_ACTION = "REAUTHORIZE_ACTION"
    #: A side effect executed and is durably recorded in the ledger but the
    #: graph checkpoint does not reflect it → restore the recorded result
    #: (never re-execute) and re-verify.
    RESTORE_RECORDED_RESULT = "RESTORE_RECORDED_RESULT"
    #: Execution may have occurred but no durable record proves the outcome;
    #    stop safely and report uncertainty (never replay).
    BLOCK_UNKNOWN_OUTCOME = "BLOCK_UNKNOWN_OUTCOME"
    #: Checkpoint/task state is corrupt or unreadable; fail closed.
    BLOCK_CORRUPT_STATE = "BLOCK_CORRUPT_STATE"


@dataclass
class ReconciliationResult:
    decision: RecoveryDecision
    patches: dict[str, Any]
    outcome_note: str
    should_block: bool = False


def reconcile_resume(
    state: Mapping[str, Any],
    runtime: TaskRuntime,
    task_record: Any | None = None,
) -> ReconciliationResult:
    """Classify the restart situation from durable/observable evidence only.

    Pure function: reads the workflow checkpoint state, the durable execution
    ledger (via ``runtime.executor.recorded_result``), and the durable task
    record. Never consults model prose. Returns state patches to apply before
    resuming the graph, or a blocking decision.
    """
    if not isinstance(state, dict):
        return ReconciliationResult(
            RecoveryDecision.BLOCK_CORRUPT_STATE,
            {},
            "checkpoint state is not a valid mapping; cannot resume safely",
            should_block=True,
        )

    # Phase 2: an uncertain outcome already decided → block, never replay.
    if state.get("outcome_uncertain"):
        return ReconciliationResult(
            RecoveryDecision.BLOCK_UNKNOWN_OUTCOME,
            {},
            "prior side effect not durably recorded; manual reconciliation required",
            should_block=True,
        )

    executions = list(state.get("executions") or [])
    pending_auth = state.get("pending_authorized_id")
    proposals = list(state.get("proposals") or [])
    execution_missing = state.get("execution_missing", False)

    # ------------------------------------------------------------------ Case B
    # Artifact was issued (authorize granted) but execute not yet called.
    # ``execute`` consumes ``pending_authorized_id`` (``runtime.take()``) as
    # its first action, so if it is still present the tool definitely did not
    # run. On restart the in-memory issued table is empty, so a stale
    # artifact can never be executed - it must be revalidated and
    # reauthorized under current permissions.
    if pending_auth is not None:
        # Double safety: if the ledger already holds this action (crash gap
        # between execute's side effect and its ledger.put), restore the
        # recorded result instead of re-executing.
        if proposals:
            recorded = runtime.executor.recorded_result(proposals[0].fingerprint())
            if recorded is not None:
                patches: dict[str, Any] = {
                    "executions": [recorded],
                    "pending_authorized_id": None,
                    "execution_missing": False,
                    "last_failure": None,
                    "task_status": TaskStatus.RUNNING,
                }
                return ReconciliationResult(
                    RecoveryDecision.RESTORE_RECORDED_RESULT,
                    patches,
                    "restored durably recorded execution result; will re-verify",
                )
        # No execution recorded → the artifact was never consumed → safe to
        # invalidate and re-authorize under current permissions.
        reauth_patches: dict[str, Any] = {
            "pending_authorized_id": None,
            "execution_missing": False,
            "last_failure": {
                "category": Failure.AUTHORIZATION_DENIED.value,
                "message": "pre-restart authorization artifact unusable after restart",
                "task_id": str(state.get("task_id", "unknown")),
                "step_id": state.get("current_step", 0),
                "model_id": state.get("model_id"),
                "at": datetime.now(UTC).isoformat(),
            },
        }
        return ReconciliationResult(
            RecoveryDecision.REAUTHORIZE_ACTION,
            reauth_patches,
            "pre-restart authorization artifact is unusable; revalidate and reauthorize",
        )

    # ------------------------------------------------------------------ Case C/D
    # ``execute`` always either records an ``ExecutionResult`` or sets
    # ``execution_missing=True`` before returning, so if neither is present
    # the tool did not run. When execute *was* called but the checkpoint lost
    # its result (crash gap between the tool running and the graph
    # checkpointing), the durable ledger is the arbiter: if it holds the
    # action, restore the recorded result; otherwise there is nothing to
    # restore and we resume planning (the ledger's duplicate check protects
    # against any identical replay).
    if executions or execution_missing:
        return ReconciliationResult(
            RecoveryDecision.RESUME_PLANNING,
            {},
            "durable execution state consistent; resuming planning",
        )

    if proposals:
        recorded = runtime.executor.recorded_result(proposals[0].fingerprint())
        if recorded is not None:
            patches = {
                "executions": [recorded],
                "pending_authorized_id": None,
                "execution_missing": False,
                "last_failure": None,
                "task_status": TaskStatus.RUNNING,
            }
            return ReconciliationResult(
                RecoveryDecision.RESTORE_RECORDED_RESULT,
                patches,
                "restored durably recorded execution result; will re-verify",
            )
        # 3.1 crash gap: an intent was claimed but no result was recorded.
        # The tool may or may not have run → UNKNOWN → stop safely.
        intent = runtime.executor.get_intent(proposals[0].fingerprint())
        if intent is not None and intent.status is IntentStatus.EXECUTION_STARTED:
            return ReconciliationResult(
                RecoveryDecision.BLOCK_UNKNOWN_OUTCOME,
                {},
                "execution started but no durable result: side effect may have "
                "occurred; blocked pending manual reconciliation",
                should_block=True,
            )

    # ------------------------------------------------------------------ Case A
    # No in-flight action: execute was never called, no side effect
    # possible. Resume from the last checkpointed plan step.
    return ReconciliationResult(
        RecoveryDecision.RESUME_PLANNING, {}, "no in-flight action; resume planning"
    )


__all__ = [
    "RecoveryDecision",
    "ReconciliationResult",
    "reconcile_resume",
]
