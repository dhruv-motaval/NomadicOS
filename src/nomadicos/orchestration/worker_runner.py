"""Bounded worker execution (Phase 4C, SPEC §30).

Enforces a REAL wall-clock timeout around worker execution, reaches
cancellation into supported child tasks/processes via a cleanup callback,
and tracks an honest worker lifecycle owned by trusted runtime code.

Guarantees:

- ``timeout_s`` is enforced with a real clock, not a suggestion: the worker
  task is cancelled at the deadline and given a bounded grace period to die.
- Cleanup (child-process kill) is awaited and itself time-bounded.
- External cancellation (mission abort) is NEVER swallowed: the
  ``CancelledError`` propagates after cleanup so the caller cannot mistake a
  cancel for success (SPEC §30).
- A worker that survives the cancel grace is declared ``OUTCOME_UNKNOWN``:
  its outcome (and any side effects) cannot be established, so the caller
  must block replays rather than retry blindly (SPEC §56.14).
- Side-effect safety: NomadicOS workers have NO direct execution channel —
  every side effect travels the validate -> authorize -> execute harness
  AFTER the worker returns. A worker that times out before returning
  therefore integrated zero proposals, and a retry cannot double-execute
  anything the durable execution-intent ledger has not already fenced.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nomadicos.contracts.worker import WorkerLifecycle, WorkerStatus
from nomadicos.orchestration.worker import WorkerAssignment, WorkerResult

#: bound on the cleanup callback itself (kill operations are fast; this
#: guards against a wedged supervisor call)
CLEANUP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class WorkerRun:
    """One bounded worker execution: result + lifecycle, correlated to the
    assignment's task/mission identity (untrusted result, trusted lifecycle)."""

    result: WorkerResult
    lifecycle: WorkerLifecycle
    elapsed_s: float


def _result_for(
    assignment: WorkerAssignment,
    *,
    worker: str,
    status: WorkerStatus,
    failure_category: str,
    diagnostics: str,
) -> WorkerResult:
    return WorkerResult(
        worker=worker,
        role=assignment.role,
        task_id=assignment.task_id,
        mission_id=assignment.mission_id,
        step_id=assignment.step_id,
        status=status,
        failure_category=failure_category,
        diagnostics=diagnostics[:500],
        actions_attempted=0,
        elapsed_s=0.0,
    )


async def _bounded_cleanup(cleanup: Callable[[], Any] | None) -> None:
    """Run the cleanup callback with a hard time bound. Never raises: a
    cleanup failure is recorded by the caller, not propagated as a worker
    success/failure."""
    if cleanup is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(cleanup), CLEANUP_TIMEOUT_S)
    except TimeoutError:
        return  # cleanup exceeded its bound; diagnostics already recorded
    except Exception:
        return  # cleanup is best-effort; the terminal status stands


async def run_worker_bounded(
    execute: Callable[[WorkerAssignment], Awaitable[WorkerResult]],
    assignment: WorkerAssignment,
    *,
    worker_name: str,
    timeout_s: float,
    cleanup: Callable[[], Any] | None = None,
    cancel_grace_s: float = 5.0,
) -> WorkerRun:
    """Execute one worker assignment under a real wall-clock timeout.

    Lifecycle transitions (trusted code only):

    - worker returns within ``timeout_s`` -> COMPLETED / FAILED per result
    - deadline fires -> CANCEL_REQUESTED -> worker cancelled within grace
      -> TIMED_OUT (retry is safe: no proposals were integrated)
    - deadline fires and the worker SURVIVES the cancel grace ->
      OUTCOME_UNKNOWN (uncertain; the caller must block any replay)
    - external CancelledError -> cleanup, lifecycle CANCELLED, re-raise
      (never swallowed, never reported as success)
    """
    started = time.monotonic()
    task: asyncio.Task[WorkerResult] = asyncio.ensure_future(execute(assignment))
    shielded: asyncio.Future[WorkerResult] = asyncio.shield(task)
    try:
        result = await asyncio.wait_for(shielded, timeout_s)
        elapsed = time.monotonic() - started
        lifecycle = (
            WorkerLifecycle.COMPLETED
            if result.status is WorkerStatus.SUCCEEDED
            else WorkerLifecycle.FAILED
        )
        result.elapsed_s = elapsed
        return WorkerRun(result=result, lifecycle=lifecycle, elapsed_s=elapsed)
    except TimeoutError:
        # Deadline fired. The shield absorbed wait_for's cancel, so the
        # worker task is (possibly) still running.
        if task.done() and not task.cancelled():
            # Completed exactly under the wire: use the honest result.
            result = task.result()
            result.elapsed_s = time.monotonic() - started
            lifecycle = (
                WorkerLifecycle.COMPLETED
                if result.status is WorkerStatus.SUCCEEDED
                else WorkerLifecycle.FAILED
            )
            return WorkerRun(result=result, lifecycle=lifecycle, elapsed_s=result.elapsed_s)
        # CANCEL_REQUESTED -> cancel the worker and wait a bounded grace.
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), cancel_grace_s)
        except TimeoutError:
            # Uncancellable: outcome cannot be established. Detach the task
            # (swallow its eventual exception) and report OUTCOME_UNKNOWN.
            task.add_done_callback(_swallow_task_exception)
            await _bounded_cleanup(cleanup)
            return WorkerRun(
                result=_result_for(
                    assignment,
                    worker=worker_name,
                    status=WorkerStatus.TIMED_OUT,
                    failure_category="OUTCOME_UNKNOWN",
                    diagnostics=(
                        f"worker survived cancellation for {cancel_grace_s}s; "
                        "outcome uncertain - replays must be blocked"
                    ),
                ),
                lifecycle=WorkerLifecycle.OUTCOME_UNKNOWN,
                elapsed_s=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            # Distinguish WHO was cancelled: if OUR runner task has a
            # pending cancellation this is an EXTERNAL abort (mission stop)
            # and must propagate; otherwise the CancelledError came from the
            # awaited worker task dying from the cancel WE requested.
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                await _bounded_cleanup(cleanup)
                raise
        # Worker died within the grace: cleanup child processes, TIMED_OUT.
        await _bounded_cleanup(cleanup)
        return WorkerRun(
            result=_result_for(
                assignment,
                worker=worker_name,
                status=WorkerStatus.TIMED_OUT,
                failure_category="WORKER_TIMEOUT",
                diagnostics=f"worker exceeded {timeout_s}s wall-clock budget",
            ),
            lifecycle=WorkerLifecycle.TIMED_OUT,
            elapsed_s=time.monotonic() - started,
        )
    except asyncio.CancelledError:
        # Distinguish WHO cancelled: if OUR runner task has a pending
        # cancellation this is an EXTERNAL abort (mission stop) that must
        # propagate after cleanup — never swallowed, never success (SPEC §30).
        # If the worker itself raised CancelledError with no external cancel
        # pending, that is the worker's own cancellation signal: record the
        # honest terminal CANCELLED state (failure, not success).
        current = asyncio.current_task()
        external = current is None or current.cancelling() > 0
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), cancel_grace_s)
        except (TimeoutError, asyncio.CancelledError):
            if not task.cancelled():
                task.add_done_callback(_swallow_task_exception)
        await _bounded_cleanup(cleanup)
        if external:
            raise
        return WorkerRun(
            result=_result_for(
                assignment,
                worker=worker_name,
                status=WorkerStatus.FAILED,
                failure_category="WORKER_CANCELLED",
                diagnostics="worker raised CancelledError; terminal CANCELLED",
            ),
            lifecycle=WorkerLifecycle.CANCELLED,
            elapsed_s=time.monotonic() - started,
        )
    except Exception as exc:
        # Worker crashed (bug / injected fault): honest FAILED result, never
        # a silent swallow and never a raw exception leaking into gather.
        return WorkerRun(
            result=_result_for(
                assignment,
                worker=worker_name,
                status=WorkerStatus.FAILED,
                failure_category="WORKER_CRASH",
                diagnostics=f"worker crashed: {type(exc).__name__}: {exc}"[:500],
            ),
            lifecycle=WorkerLifecycle.FAILED,
            elapsed_s=time.monotonic() - started,
        )


def _swallow_task_exception(task: asyncio.Task) -> None:
    """Detached-task callback: consume an abandoned worker's eventual
    exception so asyncio does not log 'exception was never retrieved'."""
    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
