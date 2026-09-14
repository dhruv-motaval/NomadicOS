"""Multi-agent orchestrator (ADR-0030).

Planner decomposes the goal into a subtask DAG → waves of independent subtasks
execute as parallel worker agents (each with its own identity, budget, model
selection, and Security Gate mediation) → synthesizer merges verified results.

Safety: every agent — planner, workers, synthesizer — is still mediated by the
same Security Gate (BP §364/§375: no multi-agent bypass, I5). Bounded by plan
size + per-agent budgets + total budget (I10). Planner failure degrades
gracefully to single-agent mode (BP §237).
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from nomadicos.agent.roles import PLANNER_ROLE, SYNTHESIZER_ROLE, WORKER_ROLE
from nomadicos.agent.runtime import AgentRuntime, TaskReport
from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSeverity,
    AuditSink,
)
from nomadicos.core.errors import ModelFailure, NomadicError
from nomadicos.core.logging import get_logger
from nomadicos.models.base import GenerateRequest, LocalModel
from nomadicos.security.permissions import SubjectIdentity

logger = get_logger("agent.orchestrator")

MAX_SUBTASKS = 6
_VALID_TASK_TYPES = {
    "coding",
    "reasoning",
    "research",
    "instruction_following",
    "tool_use",
    "long_context",
    "general_generation",
    "knowledge_lookup",
    "summarization",
    "planning",
    "automation",
    "analysis",
    "creative_generation",
    "mixed",
}


@dataclass
class SubtaskResult:
    subtask_id: str
    description: str
    status: str  # SUCCESS / FAILED / SKIPPED
    report: TaskReport | None = None
    error: str | None = None


@dataclass
class OrchestrationResult:
    goal: str
    final_status: str  # SUCCESS / PARTIALLY_COMPLETED / FAILED
    subtask_results: list[SubtaskResult] = field(default_factory=list)
    synthesis: str = ""
    duration_seconds: float = 0.0


class Orchestrator:
    def __init__(
        self,
        planner: LocalModel,
        audit_sink: AuditSink,
        runtime_factory: Any,
        *,
        max_subtasks: int = MAX_SUBTASKS,
    ) -> None:
        self._planner_model = planner
        self._audit = audit_sink
        self._runtime_factory = runtime_factory
        self._max_subtasks = max_subtasks

    # ------------------------------------------------------------------ plan

    async def plan(self, goal: str, identity: SubjectIdentity) -> list[dict[str, Any]]:
        """Planner agent decomposes the goal into a validated subtask DAG."""
        prompt = f"{PLANNER_ROLE.directive}\n\nGOAL: {goal}"
        request = GenerateRequest(
            prompt=prompt,
            max_output_tokens=PLANNER_ROLE.max_output_tokens,
            temperature=PLANNER_ROLE.temperature,
        )
        raw = await self._planner_model.generate(request)
        subtasks = self._parse_plan(raw.text)
        await self._audit_plan(identity, goal, subtasks)
        return subtasks

    def _parse_plan(self, text: str) -> list[dict[str, Any]]:
        start, end = text.find("{"), text.rfind("}") + 1
        if start == -1 or end == 0:
            raise ModelFailure("planner returned no JSON plan")
        try:
            payload = json.loads(text[start:end])
        except json.JSONDecodeError as exc:
            raise ModelFailure("planner JSON invalid") from exc
        subtasks = payload.get("subtasks") or []
        if not 1 <= len(subtasks) <= MAX_SUBTASKS:
            raise ModelFailure(f"plan size out of bounds: {len(subtasks)}")
        seen: set[str] = set()
        ordered_ids: list[str] = []
        for subtask in subtasks:
            sid = str(subtask.get("id", "")).strip()
            if not sid or sid in seen:
                raise ModelFailure("subtask ids must be unique and non-empty")
            seen.add(sid)
            ordered_ids.append(sid)
            if str(subtask.get("task_type", "")).lower() not in _VALID_TASK_TYPES:
                raise ModelFailure(f"invalid task_type in subtask {sid}")
            deps = subtask.get("depends_on") or []
            if not isinstance(deps, list) or any(d not in seen for d in deps):
                # dependencies may only reference EARLIER ids ⇒ acyclic by construction
                raise ModelFailure(f"subtask {sid} has invalid dependencies")
            if not str(subtask.get("description", "")).strip():
                raise ModelFailure(f"subtask {sid} has empty description")
        return subtasks[: self._max_subtasks]

    # ------------------------------------------------------------ orchestrate

    async def orchestrate(
        self,
        goal: str,
        identity: SubjectIdentity,
        *,
        max_subtasks: int = MAX_SUBTASKS,
        user_confirmation: bool = False,
    ) -> OrchestrationResult:
        """Plan → execute the DAG in dependency waves → synthesize (ADR-0030)."""
        started = time.monotonic()
        self._max_subtasks = max_subtasks

        try:
            subtasks = await self.plan(goal, identity)
        except (ModelFailure, NomadicError) as exc:
            logger.warning("planning failed (%s) — degrading to single-agent", exc)
            fallback = self._runtime_factory("fallback", WORKER_ROLE)
            report = await fallback.execute_task(goal, identity)
            return OrchestrationResult(
                goal=goal,
                final_status=report.status.value,
                synthesis=report.render(),
                duration_seconds=time.monotonic() - started,
            )

        results: dict[str, SubtaskResult] = {}
        pending = {s["id"]: s for s in subtasks}
        all_ids = [s["id"] for s in subtasks]

        # Dependency-wave execution (ADR-0030: independent subtasks run parallel).
        while pending:
            ready = [
                s
                for s in pending.values()
                if all(
                    d in results and results[d].status == "SUCCESS" for d in s.get("depends_on", [])
                )
            ]
            if not ready:
                # remaining subtasks are blocked by failed upstream work
                for sid, subtask in pending.items():
                    results[sid] = SubtaskResult(
                        subtask_id=sid,
                        description=subtask["description"],
                        status="SKIPPED",
                        error="upstream dependency failed",
                    )
                    await self._audit_subtask(identity, sid, "SKIPPED")
                break

            wave = [pending.pop(s["id"]) for s in ready]
            outcomes = await asyncio.gather(*(self._run_worker(s, identity, goal) for s in wave))
            for subtask, (report, _) in zip(wave, outcomes, strict=True):
                results[subtask["id"]] = SubtaskResult(
                    subtask_id=subtask["id"],
                    description=subtask["description"],
                    status=report.status.value,
                    report=report,
                    error="; ".join(report.failed) if report.failed else None,
                )

        # Failure cascade: any FAILED/SKIPPED subtask ⇒ final PARTIAL/FAILED.
        statuses = [r.status for r in results.values()]
        final_status = (
            "SUCCESS"
            if all(s == "SUCCESS" for s in statuses)
            else "PARTIALLY_COMPLETED"
            if any(s == "SUCCESS" for s in statuses)
            else "FAILED"
        )

        synthesis = await self._synthesize(goal, results, identity)

        return OrchestrationResult(
            goal=goal,
            final_status=final_status,
            subtask_results=[results[sid] for sid in all_ids],
            synthesis=synthesis,
            duration_seconds=time.monotonic() - started,
        )

    # ---------------------------------------------------------------- workers

    async def _run_worker(
        self, subtask: dict[str, Any], identity: SubjectIdentity, goal: str
    ) -> tuple[TaskReport, Any]:
        """One worker agent = one AgentRuntime bound to one subtask (ADR-0030)."""
        agent_id = f"{identity.user_id}/{identity.task_id}/worker-{subtask['id']}"
        runtime: AgentRuntime = self._runtime_factory(agent_id, WORKER_ROLE)
        worker_identity = SubjectIdentity(
            user_id=identity.user_id,
            session_id=identity.session_id,
            task_id=f"{identity.task_id}-{subtask['id']}" if identity.task_id else None,
            run_id=identity.run_id,
        )
        directive_goal = (
            f"Owner goal: {goal}\nYour subtask: {subtask['description']}\n"
            f"Complete this subtask using tools; report truthfully."
        )
        await self._audit_subtask(identity, subtask["id"], "STARTED")
        report: TaskReport
        try:
            report = await runtime.execute_task(directive_goal, worker_identity)
        except NomadicError as exc:
            logger.warning("worker failed subtask=%s error=%s", subtask["id"], exc)
            report = TaskReport(
                task_id=identity.task_id or subtask["id"],
                goal=subtask["description"],
                status=__import__(
                    "nomadicos.core.lifecycle", fromlist=["TaskStatus"]
                ).TaskStatus.FAILED,
                requested=subtask["description"],
                failed=[str(exc)],
            )
            await self._audit_subtask(identity, subtask["id"], report.status.value)
        return report, None

    async def _synthesize(
        self, goal: str, results: dict[str, SubtaskResult], identity: SubjectIdentity
    ) -> str:
        """Synthesizer agent merges verified worker outputs."""
        runtime: AgentRuntime = self._runtime_factory(
            f"{identity.user_id}/{identity.task_id}/synth", SYNTHESIZER_ROLE
        )
        lines = [
            f"{sid} [{r.status}]: {r.description} — "
            + ("; ".join(r.report.completed) if r.report else (r.error or ""))
            for sid, r in results.items()
        ]
        prompt = (
            f"{SYNTHESIZER_ROLE.directive}\n\nOwner goal: {goal}\n"
            f"Worker results:\n" + "\n".join(lines)
        )
        try:
            report = await runtime.execute_task(prompt, identity)
            return report.render() if report.completed else report.requested
        except NomadicError as exc:
            logger.warning("synthesis degraded: %s", exc)
            return "\n".join(lines)

    # ------------------------------------------------------------------ audit

    async def _audit_plan(
        self, identity: SubjectIdentity, goal: str, subtasks: list[dict[str, Any]]
    ) -> None:
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TASK_EVENT,
                severity=AuditSeverity.INFO,
                user_id=identity.user_id,
                session_id=identity.session_id,
                task_id=identity.task_id,
                subject="planner",
                decision=f"plan accepted ({len(subtasks)} subtasks)",
                reason=goal[:200],
            )
        )

    async def _audit_subtask(
        self, identity: SubjectIdentity, subtask_id: str, decision: str
    ) -> None:
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TASK_EVENT,
                severity=AuditSeverity.INFO,
                user_id=identity.user_id,
                session_id=identity.session_id,
                task_id=identity.task_id,
                run_id=identity.run_id,
                step_id=subtask_id,
                subject="orchestrator",
                decision=decision,
            )
        )


__all__ = ["OrchestrationResult", "Orchestrator", "SubtaskResult"]
