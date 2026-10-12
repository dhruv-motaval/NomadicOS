"""Memory runtime hooks (Phase 11G) — the ONLY seams where the task runtime
touches memory. DATA only (SPEC §32, §52F/G):

- seed/update working memory at deterministic lifecycle points (read-only
  over TaskState; ephemeral, task-isolated);
- write ONE episodic record at task end, from REAL verification artifacts
  and execution evidence only (11C API) — model claims, critic ACCEPT, and
  bare executor success can never produce a verified episode;
- emit MEMORY_UPDATED only for an actual durable write (never for a
  failed write, never for reads).

Failure semantics are absolute here: a memory problem returns a note and
never alters TaskStatus, GoalVerifier outcomes, routing, or task success.
"""

from __future__ import annotations

from typing import Any

from nomadicos.contracts.verification import VerificationLevel
from nomadicos.kernel.events import EventType
from nomadicos.memory.episodic import record_from_task_result
from nomadicos.memory.runtime import RuntimeMemory


def seed_working(memory: RuntimeMemory, task_id: str, goal_text: str) -> None:
    """Pre-task deterministic point: current goal into task-isolated
    ephemeral working memory."""
    try:
        memory.working.set_goal(task_id, goal_text)
    except Exception:  # noqa: BLE001 - secondary context, isolated
        pass


def update_working_from_state(memory: RuntimeMemory, task_id: str, state: dict) -> None:
    """Post-task working-memory refresh from the final TaskState snapshot
    (plan digest, recent observations/failures, critic feedback)."""
    try:
        working = memory.working
        goal = state.get("goal")
        if goal is not None:
            working.set_goal(task_id, goal.objective)
        digest = " > ".join(str(s.description) for s in (state.get("plan") or [])[:8])
        if digest:
            working.set_plan_digest(task_id, digest[:400])
        for obs in (state.get("observations") or [])[-3:]:
            working.push_observation(task_id, str(getattr(obs, "summary", ""))[:200])
        for fail in (state.get("failures") or [])[-3:]:
            working.push_failure(task_id, f"{fail.category.value}: {fail.message}"[:160])
        critic = state.get("critic_feedback")
        if critic:
            major = "; ".join(critic.get("major_issues") or [])[:180]
            working.set_critic_feedback(task_id, f"critic: {major}")
    except Exception:  # noqa: BLE001 - ephemeral context failure is isolated
        pass


def episodic_hook(memory: RuntimeMemory, task_id: str, state: dict[str, Any]) -> str:
    """End-of-task episodic write from REAL verification artifacts.

    Verified status comes ONLY from the actual GOAL VerificationResult in
    state plus the task lifecycle status (11C gate). Failure semantics: a
    memory problem is isolated to the returned note — TaskStatus, GoalVerifier
    outcomes, routing, and owner state are untouched, and NO MEMORY_UPDATED
    event is emitted for a failed write."""
    goal_artifact = None
    for v in state.get("verifications") or []:
        if v.level is VerificationLevel.GOAL:
            goal_artifact = v  # the last GOAL-level verifier artifact
    status = state.get("task_status")
    goal = state.get("goal")
    executions = list(state.get("executions") or [])
    had_evidence = goal_artifact is not None or bool(executions)
    try:
        record = record_from_task_result(
            memory.store,
            task_id=task_id,
            objective=goal.objective if goal is not None else "",
            goal_verdict=goal_artifact,
            task_status=status,
            executions=len(executions),
            test_exits=[e.exit_code for e in executions if e.exit_code is not None][-6:],
            evidence_refs=[f"verif:{goal_artifact.id}"] if goal_artifact is not None else [],
        )
    except Exception:  # noqa: BLE001 - memory boundary isolation
        return "memory: write failed"
    if record is not None:
        if memory.logger is not None:
            try:
                memory.logger.log(
                    EventType.MEMORY_UPDATED,
                    task_id=task_id,
                    result="episodic",
                    payload={"kind": record.kind.value, "record_id": record.id},
                )
            except Exception:  # noqa: BLE001 - event emission never breaks the task
                pass
        return f"episodic:{record.id}"
    if had_evidence:
        return "memory: write failed"  # attempted, not persisted: observable
    return ""  # nothing observable happened (claims only)
