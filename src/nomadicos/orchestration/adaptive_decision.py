"""Adaptive decision cycle (Phase 4D, SPEC §4D).

NomadicBrain observes outcomes, recognizes when the current plan is
inadequate, and decides whether to continue, retry, replan, escalate, or
stop.

The runtime retains authority over plan validation, execution, authorization,
budgets, persistence, recovery, and goal verification.  The adaptive layer
proposes decisions; trusted runtime code validates them before any state
change.  Model-generated decisions are untrusted proposals — they cannot
directly authorize execution, mutate trusted state, or bypass authority.

Every AdaptiveDecision is:
- untrusted data until validated by trusted runtime code (this module),
- attributable to a mission/task via correlation fields,
- bounded by retry/replan budgets tracked in trusted state,
- never able to grant itself permissions, change the mission objective,
  mark the mission successful, or bypass authorization.
"""

from __future__ import annotations

from typing import Any

from nomadicos.brain.planner.task_graph import TaskNodeStatus
from nomadicos.contracts.adaptive_decision import (
    AdaptiveContext,
    AdaptiveDecision,
    Decision,
    DecisionCategory,
)
from nomadicos.contracts.core import Failure, TaskStatus
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.kernel.config import BudgetConfig


class AdaptiveDecisionOrchestrator:
    """Phase 4D adaptive decision cycle.

    Decides CONTINUE / RETRY / REPLAN / ESCALATE / STOP based on trusted
    runtime state.  All counting, budgeting, and safety rules live in trusted
    code; the model (if consulted) only produces a proposal that is validated
    before use.

    The orchestrator is stateless per call: it receives the full TaskState
    and BudgetConfig, and returns a state-update dict.  The caller (graph
    node) applies the update and routes accordingly.

    An optional ``brain_engine`` (an ``EngineStructuredGenerator``) may be
    supplied for bounded model consultation.  When absent, the orchestrator
    uses deterministic rules only — which is correct and safe.
    """

    def __init__(
        self,
        *,
        budget: BudgetConfig,
        brain_engine: Any = None,
    ) -> None:
        self._budget = budget
        self._brain_engine = brain_engine

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    def decide(self, state: dict[str, Any]) -> dict[str, Any]:
        """Run one adaptive decision cycle.

        Returns a state-update dict containing ``adaptive_decision`` (the
        validated :class:`AdaptiveDecision` as a dict) plus any state fields
        the decision requires (``task_status`` for STOP/BLOCKED,
        ``outcome_note``, ``plan_revision`` for REPLAN, ``attempt`` for RETRY,
        etc.).
        Never mutates ``state`` in place.
        """
        # Fail closed: empty or missing state cannot proceed.
        if not state or not state.get("task_id"):
            return self._decide_stop(
                AdaptiveContext(
                    mission_id=state.get("task_id", "unknown"),
                    task_id=None,
                    step_id=None,
                    plan_revision=0,
                    task_status=TaskStatus.BLOCKED.value,
                    task_status_map={},
                    completed_tasks=[],
                    failed_tasks=[],
                    blocked_tasks=[],
                    pending_tasks=[],
                    last_failure_category=None,
                    last_failure_message="empty or missing task state",
                    last_execution_succeeded=None,
                    last_verdict=None,
                    last_step_verdict=None,
                    retries_used=0,
                    retries_remaining=self._budget.max_retries_per_task,
                    replans_used=0,
                    replans_remaining=self._budget.max_replans_per_mission,
                    decisions_used=0,
                    decisions_remaining=self._budget.max_adaptive_decisions_per_mission,
                    max_retries_per_task=self._budget.max_retries_per_task,
                    max_replans_per_mission=self._budget.max_replans_per_mission,
                    max_adaptive_decisions_per_mission=self._budget.max_adaptive_decisions_per_mission,
                    max_reasoning_calls_per_cycle=self._budget.max_reasoning_calls_per_cycle,
                    outcome_uncertain=True,
                    previous_decisions=[],
                    memory_references=[],
                    reasoning_calls_available=self._budget.max_reasoning_calls_per_cycle,
                ),
                DecisionCategory.INTERNAL,
                "empty or missing task state; cannot proceed",
                state,
            )

        ctx = self._build_context(state)

        # ---- 1. Deterministic safety rules first (trusted, no model) ----
        safety = self._apply_safety_rules(ctx, state)
        if safety is not None:
            return safety

        # ---- 2. Budget checks ----
        budget_decision = self._check_budgets(ctx, state)
        if budget_decision is not None:
            return budget_decision

        # ---- 3. Bounded Brain proposal when reasoning can help ----
        model_decision = self._ask_brain(ctx, state)
        if model_decision is not None:
            return model_decision

        # ---- 4. Fallback: deterministic CONTINUE when nothing is wrong ----
        return self._decide_continue(ctx)

    # ------------------------------------------------------------------ #
    # Context building (trusted state snapshot)
    # ------------------------------------------------------------------ #

    def _build_context(self, state: dict[str, Any]) -> AdaptiveContext:
        executions = list(state.get("executions") or [])
        verifications = list(state.get("verifications") or [])
        last_exec = executions[-1] if executions else None
        last_failure = state.get("last_failure") or {}

        # Goal verification verdict (only GOAL level counts)
        goal_verdict = None
        for v in reversed(verifications):
            if str(getattr(v, "level", "")).upper() == "GOAL":
                goal_verdict = v.verdict.value if hasattr(v.verdict, "value") else str(v.verdict)
                break

        # Step verification verdict
        step_verdict = None
        for v in reversed(verifications):
            if str(getattr(v, "level", "")).upper() == "STEP":
                step_verdict = v.verdict.value if hasattr(v.verdict, "value") else str(v.verdict)
                break

        previous_decisions = list(state.get("adaptive_decisions") or [])[-10:]

        # The per-task retry counter is the trusted budget state written by
        # the RETRY decision itself (task_retries[task_id], exactly once per
        # accepted retry). retries_used mirrors THAT task's counter so the
        # deterministic retry rules stay consistent with the task actually
        # receiving the decision — a mission-level scalar would under-count
        # and let rule 5b (retry budget exhausted) never fire.
        task_retries_map = dict(state.get("task_retries") or {})
        retries_used = (
            int(task_retries_map.get(str(state.get("current_task_id")), 0) or 0)
            if state.get("current_task_id") is not None
            else 0
        )

        return AdaptiveContext(
            mission_id=state.get("task_id", "unknown"),
            task_id=state.get("current_task_id"),
            step_id=None,
            plan_revision=int(state.get("plan_revision", 0) or 0),
            task_status=state.get("task_status", TaskStatus.RUNNING.value),
            task_status_map=dict(state.get("task_status_map") or {}),
            completed_tasks=[
                tid
                for tid, st in (state.get("task_status_map") or {}).items()
                if st == TaskNodeStatus.COMPLETED.value
            ],
            failed_tasks=[
                tid
                for tid, st in (state.get("task_status_map") or {}).items()
                if st == TaskStatus.FAILED.value
            ],
            blocked_tasks=[
                tid
                for tid, st in (state.get("task_status_map") or {}).items()
                if st == TaskStatus.BLOCKED.value
            ],
            pending_tasks=[
                tid
                for tid, st in (state.get("task_status_map") or {}).items()
                if st in (TaskNodeStatus.PENDING.value, TaskNodeStatus.READY.value)
            ],
            last_failure_category=last_failure.get("category"),
            last_failure_message=str(last_failure.get("message", ""))[:200],
            last_execution_succeeded=last_exec.succeeded if last_exec else None,
            last_verdict=goal_verdict,
            last_step_verdict=step_verdict,
            retries_used=retries_used,
            retries_remaining=max(
                0,
                self._budget.max_retries_per_task - retries_used,
            ),
            replans_used=int(state.get("replans_used", 0) or 0),
            replans_remaining=max(
                0,
                self._budget.max_replans_per_mission - int(state.get("replans_used", 0) or 0),
            ),
            decisions_used=int(state.get("adaptive_decisions_used", 0) or 0),
            decisions_remaining=max(
                0,
                self._budget.max_adaptive_decisions_per_mission
                - int(state.get("adaptive_decisions_used", 0) or 0),
            ),
            max_retries_per_task=self._budget.max_retries_per_task,
            max_replans_per_mission=self._budget.max_replans_per_mission,
            max_adaptive_decisions_per_mission=self._budget.max_adaptive_decisions_per_mission,
            max_reasoning_calls_per_cycle=self._budget.max_reasoning_calls_per_cycle,
            outcome_uncertain=bool(state.get("outcome_uncertain", False)),
            previous_decisions=previous_decisions,
            memory_references=list(state.get("memory_references") or []),
            reasoning_calls_available=self._budget.max_reasoning_calls_per_cycle,
        )

    # ------------------------------------------------------------------ #
    # Deterministic safety rules (fail closed, no model)
    # ------------------------------------------------------------------ #

    def _apply_safety_rules(
        self, ctx: AdaptiveContext, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Rule 0: if goal is missing or invalid, STOP
        if not state.get("goal"):
            return self._decide_stop(
                ctx,
                DecisionCategory.INTERNAL,
                "goal decomposition is missing or invalid",
                state,
            )

        # Rule 1: OUTCOME_UNKNOWN blocks unsafe retries / conflicting
        # operations until reconciled (SPEC §56.14).
        if ctx.outcome_uncertain:
            return self._decide_stop(
                ctx,
                DecisionCategory.OUTCOME_UNKNOWN,
                "outcome uncertain: side-effect durability not proven; "
                "blocking replay until reconciled",
                state,
            )

        # Rule 2: resource exhaustion → typed observable outcome, never a
        # silent skip or unbounded retry.
        if ctx.decisions_used >= ctx.max_adaptive_decisions_per_mission:
            return self._decide_stop(
                ctx,
                DecisionCategory.BUDGET_EXHAUSTED,
                f"adaptive decision budget exhausted: "
                f"{ctx.decisions_used}/{ctx.max_adaptive_decisions_per_mission}",
                state,
            )

        # Rule 3: authorization denials cannot be reclassified as transient
        # failures simply because a model suggests retrying.
        if ctx.last_failure_category == Failure.AUTHORIZATION_DENIED.value:
            return self._decide_stop(
                ctx,
                DecisionCategory.AUTHORIZATION_DENIED,
                "authorization denied; not reclassified as retryable",
                state,
            )

        # Rule 3b: MODEL_ERROR / RESOURCE_UNAVAILABLE -> ESCALATE — but only
        # when RECOVERY has not already escalated for this failure. Recovery
        # escalates first (clears model_id, adds tried_models); a second
        # ESCALATE here would double-count the escalation budget (§14) and
        # exhaust it before the next model can even be selected.
        if (
            ctx.last_failure_category
            in (
                Failure.MODEL_ERROR.value,
                Failure.RESOURCE_UNAVAILABLE.value,
            )
            and state.get("model_id") is not None
        ):
            return self._decision_to_update(
                AdaptiveDecision(
                    decision=Decision.ESCALATE,
                    category=DecisionCategory.MODEL_ERROR
                    if ctx.last_failure_category == Failure.MODEL_ERROR.value
                    else DecisionCategory.RESOURCE_UNAVAILABLE,
                    rationale=f"failure category {ctx.last_failure_category}: escalate",
                    mission_id=ctx.mission_id,
                    task_id=ctx.task_id,
                    step_id=ctx.step_id,
                    correlation_id=ctx.mission_id,
                    correlation_type="mission",
                    supporting_evidence=[],
                    expected_effect="escalate to a stronger model / more resources",
                    retries_used=ctx.retries_used,
                    retries_remaining=ctx.retries_remaining,
                    replans_used=ctx.replans_used,
                    replans_remaining=ctx.replans_remaining,
                    decisions_used=ctx.decisions_used,
                    decisions_remaining=ctx.decisions_remaining,
                ),
                ctx,
                state,
            )

        # Rule 4: if the goal is already verifiably satisfied, CONTINUE is
        # meaningless — but only the goal verifier establishes SUCCESS.
        if ctx.last_verdict == VerificationOutcome.PASS.value:
            return self._decide_stop(
                ctx,
                DecisionCategory.PLAN_COMPLETE,
                "goal already verified as PASS; no further action needed",
                state,
            )

        # Rule 5: if the goal verification failed and replans remain, REPLAN.
        if (
            ctx.last_verdict == VerificationOutcome.NOT_PASS.value
            and ctx.replans_used < ctx.max_replans_per_mission
        ):
            return self._decision_to_update(
                AdaptiveDecision(
                    decision=Decision.REPLAN,
                    category=DecisionCategory.PLAN_COMPLETE,
                    rationale="goal verification failed; replanning within budget",
                    mission_id=ctx.mission_id,
                    task_id=ctx.task_id,
                    step_id=ctx.step_id,
                    correlation_id=ctx.mission_id,
                    correlation_type="mission",
                    supporting_evidence=[],
                    expected_effect="replan to achieve goal",
                    retries_used=ctx.retries_used,
                    retries_remaining=ctx.retries_remaining,
                    replans_used=ctx.replans_used,
                    replans_remaining=ctx.replans_remaining,
                    decisions_used=ctx.decisions_used,
                    decisions_remaining=ctx.decisions_remaining,
                    plan_revision=ctx.plan_revision + 1,
                ),
                ctx,
                state,
            )

        # Rule 5: if last execution failed and retries remain, RETRY.
        if (
            state.get("executions")
            and state["executions"][-1].status == ExecutionStatus.FAILED
            and ctx.retries_used < ctx.max_retries_per_task
        ):
            return self._decision_to_update(
                AdaptiveDecision(
                    decision=Decision.RETRY,
                    category=DecisionCategory.TASK_FAILED,
                    rationale="last execution failed; retry within budget",
                    mission_id=ctx.mission_id,
                    task_id=ctx.task_id,
                    step_id=ctx.step_id,
                    correlation_id=ctx.mission_id,
                    correlation_type="mission",
                    supporting_evidence=[],
                    expected_effect="re-execute failed task within retry budget",
                    retries_used=ctx.retries_used,
                    retries_remaining=ctx.retries_remaining,
                    replans_used=ctx.replans_used,
                    replans_remaining=ctx.replans_remaining,
                    decisions_used=ctx.decisions_used,
                    decisions_remaining=ctx.decisions_remaining,
                ),
                ctx,
                state,
            )
        # Rule 5b: if last execution failed but retries exhausted, STOP
        elif (
            state.get("executions")
            and state["executions"][-1].status == ExecutionStatus.FAILED
            and ctx.retries_used >= ctx.max_retries_per_task
        ):
            return self._decide_stop(
                ctx,
                DecisionCategory.BUDGET_EXHAUSTED,
                f"task {ctx.task_id} retry budget exhausted: "
                f"{ctx.retries_used}/{ctx.max_retries_per_task}",
                state,
            )

        return None

    def _check_budgets(self, ctx: AdaptiveContext, state: dict[str, Any]) -> dict[str, Any] | None:
        # Per-task retry budget
        task_id = ctx.task_id
        if task_id is not None:
            task_retries = int(state.get("task_retries", {}).get(task_id, 0) or 0)
            if task_retries >= ctx.max_retries_per_task:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.BUDGET_EXHAUSTED,
                    f"task {task_id} retry budget exhausted: "
                    f"{task_retries}/{ctx.max_retries_per_task}",
                    state,
                )

        # Per-mission replan budget
        if ctx.replans_used >= ctx.max_replans_per_mission:
            return self._decide_stop(
                ctx,
                DecisionCategory.BUDGET_EXHAUSTED,
                f"replan budget exhausted: {ctx.replans_used}/{ctx.max_replans_per_mission}",
                state,
            )

        return None

    # ------------------------------------------------------------------ #
    # Model proposal (bounded, validated)
    # ------------------------------------------------------------------ #

    def _ask_brain(self, ctx: AdaptiveContext, state: dict[str, Any]) -> dict[str, Any] | None:
        """Ask the Brain for a bounded proposal.  Returns a state-update
        dict or None if no Brain call is warranted.

        Only invoked when there is genuine uncertainty that reasoning could
        resolve — never as a default.  Bounded by max_reasoning_calls_per_cycle
        and the total adaptive-decision budget.
        """
        # Only ask the model when there is something genuinely undecided.
        needs_reasoning = (
            ctx.last_execution_succeeded is False
            or ctx.last_step_verdict
            in (VerificationOutcome.NOT_PASS.value, VerificationOutcome.NOT_VERIFIED.value)
            or ctx.last_verdict
            in (VerificationOutcome.NOT_PASS.value, VerificationOutcome.NOT_VERIFIED.value)
        )
        if not needs_reasoning:
            return None

        # The Brain engine (EngineStructuredGenerator) is injected via
        # constructor.  Without it, we fall back to deterministic rules only.
        engine = self._brain_engine
        if engine is None:
            return None

        from nomadicos.brain.cognition import CognitiveRequest, CognitiveRole

        # Build the proposal request.  The Brain sees only data — goal,
        # predicates, observations, failures — never authority.
        request = CognitiveRequest(
            role=CognitiveRole.DECOMPOSER,
            mission_id=ctx.mission_id,
            payload={
                "objective": state.get("goal", {}).get("objective", "")
                if isinstance(state.get("goal"), dict)
                else str(state.get("goal", "")),
                "constraints": state.get("goal", {}).get("constraints", [])
                if isinstance(state.get("goal"), dict)
                else [],
                "task_id": ctx.task_id,
                "step_id": ctx.step_id,
                "decision_context": {
                    "decision_needed": True,
                    "allowed_decisions": [d.value for d in Decision],
                    "retries_used": ctx.retries_used,
                    "retries_remaining": ctx.retries_remaining,
                    "replans_used": ctx.replans_used,
                    "replans_remaining": ctx.replans_remaining,
                    "decisions_used": ctx.decisions_used,
                    "decisions_remaining": ctx.decisions_remaining,
                    "last_failure": ctx.last_failure_category,
                    "last_verdict": ctx.last_verdict,
                    "step_verdict": ctx.last_step_verdict,
                    "completed_tasks": ctx.completed_tasks,
                    "failed_tasks": ctx.failed_tasks,
                    "blocked_tasks": ctx.blocked_tasks,
                    "pending_tasks": ctx.pending_tasks,
                    "previous_decisions": ctx.previous_decisions,
                },
            },
        )

        try:
            response = engine.generate_structured(request)
        except Exception:  # noqa: BLE001 - Brain failure → deterministic fallback
            return None

        # Parse the model's decision proposal (closed schema).
        try:
            decision = self._parse_model_decision(response.text, ctx, state)
        except Exception:  # noqa: BLE001 - malformed model output → fallback
            return None

        if decision is None:
            return None

        # Validate the decision against safety rules (the model cannot
        # bypass them).
        safety_check = self._validate_decision(decision, ctx, state)
        if safety_check is not None:
            return safety_check

        return self._decision_to_update(decision, ctx, state)

    def _parse_model_decision(
        self, raw: str, ctx: AdaptiveContext, state: dict[str, Any]
    ) -> AdaptiveDecision | None:
        import json

        try:
            obj = json.loads(raw)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None

        decision_str = obj.get("decision")
        if not isinstance(decision_str, str):
            return None
        try:
            decision = Decision(decision_str)
        except (ValueError, TypeError):
            return None

        try:
            return AdaptiveDecision(
                decision=decision,
                category=DecisionCategory(obj.get("category", DecisionCategory.INTERNAL.value)),
                rationale=str(obj.get("rationale", ""))[:400],
                mission_id=ctx.mission_id,
                task_id=obj.get("task_id") or ctx.task_id,
                step_id=obj.get("step_id") or ctx.step_id,
                correlation_id=obj.get("correlation_id") or ctx.mission_id,
                correlation_type=obj.get("correlation_type") or "mission",
                supporting_evidence=obj.get("supporting_evidence", [])[:8],
                expected_effect=str(obj.get("expected_effect", ""))[:200],
                plan_revision=obj.get("plan_revision"),
                retries_used=ctx.retries_used,
                retries_remaining=ctx.retries_remaining,
                replans_used=ctx.replans_used,
                replans_remaining=ctx.replans_remaining,
                decisions_used=ctx.decisions_used,
                decisions_remaining=ctx.decisions_remaining,
            )
        except Exception:
            return None

    def _validate_decision(
        self, decision: AdaptiveDecision, ctx: AdaptiveContext, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Trust-but-verify: the model's decision is validated against
        trusted state before being returned.  Returns a state-update dict
        or None if invalid."""
        # A STOP decision is always acceptable (it never authorizes
        # anything).
        if decision.decision is Decision.STOP:
            return self._decision_to_update(decision, ctx, state)

        # CONTINUE is acceptable only if there is actually eligible work.
        if decision.decision is Decision.CONTINUE:
            if not ctx.pending_tasks and not ctx.blocked_tasks:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.PLAN_COMPLETE,
                    "CONTINUE requested but no eligible work remains",
                    state,
                )
            return self._decision_to_update(decision, ctx, state)

        # RETRY requires a specific task and remaining retry budget.
        if decision.decision is Decision.RETRY:
            if decision.task_id is None:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.INTERNAL,
                    "RETRY requested without a task_id",
                    state,
                )
            task_retries = int(state.get("task_retries", {}).get(decision.task_id, 0) or 0)
            if task_retries >= ctx.max_retries_per_task:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.BUDGET_EXHAUSTED,
                    f"task {decision.task_id} retry budget exhausted",
                    state,
                )
            return self._decision_to_update(decision, ctx, state)

        # REPLAN requires a plan_revision and remaining replan budget.
        if decision.decision is Decision.REPLAN:
            if decision.plan_revision is None:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.INTERNAL,
                    "REPLAN requested without a plan_revision",
                    state,
                )
            if ctx.replans_used >= ctx.max_replans_per_mission:
                return self._decide_stop(
                    ctx,
                    DecisionCategory.BUDGET_EXHAUSTED,
                    "replan budget exhausted",
                    state,
                )
            return self._decision_to_update(decision, ctx, state)

        # ESCALATE requires a reason.
        if decision.decision is Decision.ESCALATE:
            if not decision.rationale.strip():
                return self._decide_stop(
                    ctx,
                    DecisionCategory.INTERNAL,
                    "ESCALATE requested without rationale",
                    state,
                )
            return self._decision_to_update(decision, ctx, state)

        return None  # unknown decision → fail closed

    # ------------------------------------------------------------------ #
    # Decision → state update
    # ------------------------------------------------------------------ #

    def _decision_to_update(
        self, decision: AdaptiveDecision, ctx: AdaptiveContext, state: dict[str, Any]
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {
            "adaptive_decision": decision.model_dump(mode="json"),
            "adaptive_decisions_used": ctx.decisions_used + 1,
            "last_adaptive_decision": decision.decision.value,
        }

        if decision.decision is Decision.STOP:
            updates["task_status"] = TaskStatus.BLOCKED.value
            updates["last_failure"] = {
                "category": decision.category.value,
                "message": decision.rationale,
                "task_id": state.get("task_id"),
                "step_id": state.get("current_step_id"),
                "model_id": state.get("model_id"),
            }
            updates["last_adaptive_decision_category"] = decision.category.value

        elif decision.decision is Decision.RETRY:
            updates["attempt"] = state.get("attempt", 1) + 1
            # bind the retry to the trusted per-task budget counter so the
            # decision budget is real state, not a suggestion (SPEC §14)
            task_retries = dict(state.get("task_retries") or {})
            tid = decision.task_id or ctx.task_id
            if tid is not None:
                task_retries[tid] = int(task_retries.get(tid, 0)) + 1
            updates["task_retries"] = task_retries
            updates["last_adaptive_decision_category"] = decision.category.value

        elif decision.decision is Decision.REPLAN:
            updates["attempt"] = state.get("attempt", 1) + 1
            updates["plan_revision"] = decision.plan_revision
            updates["replans_used"] = ctx.replans_used + 1
            updates["last_adaptive_decision_category"] = decision.category.value

        elif decision.decision is Decision.ESCALATE:
            updates["attempt"] = state.get("attempt", 1) + 1
            tried = list(state.get("tried_models") or [])
            if state.get("model_id"):
                tried = [*tried, state["model_id"]]
            updates["tried_models"] = tried
            updates["model_id"] = None  # force re-selection
            updates["escalation_count"] = [
                *(state.get("escalation_count") or []),
                {
                    "from": state.get("model_id"),
                    "reason": decision.category.value,
                    "count": len(state.get("escalation_count") or []) + 1,
                },
            ]
            updates["last_adaptive_decision_category"] = decision.category.value

        elif decision.decision is Decision.CONTINUE:
            updates["last_adaptive_decision_category"] = decision.category.value
            updates["task_status"] = state.get("task_status", TaskStatus.RUNNING.value)

        return updates

    def _decide_continue(self, ctx: AdaptiveContext) -> dict[str, Any]:
        decision = AdaptiveDecision(
            decision=Decision.CONTINUE,
            category=DecisionCategory.PLAN_COMPLETE
            if not ctx.pending_tasks
            else DecisionCategory.INTERNAL,
            rationale="no blocking condition; proceeding with eligible work",
            mission_id=ctx.mission_id,
            task_id=ctx.task_id,
            step_id=ctx.step_id,
            correlation_id=ctx.mission_id,
            correlation_type="mission",
            supporting_evidence=[],
            expected_effect="proceed with next eligible task",
            retries_used=ctx.retries_used,
            retries_remaining=ctx.retries_remaining,
            replans_used=ctx.replans_used,
            replans_remaining=ctx.replans_remaining,
            decisions_used=ctx.decisions_used,
            decisions_remaining=ctx.decisions_remaining,
        )
        return self._decision_to_update(decision, ctx, {})

    def _decide_stop(
        self,
        ctx: AdaptiveContext,
        category: DecisionCategory,
        rationale: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        decision = AdaptiveDecision(
            decision=Decision.STOP,
            category=category,
            rationale=rationale,
            mission_id=ctx.mission_id,
            task_id=ctx.task_id,
            step_id=ctx.step_id,
            correlation_id=ctx.mission_id,
            correlation_type="mission",
            supporting_evidence=[],
            expected_effect="mission halted; no further action",
            retries_used=ctx.retries_used,
            retries_remaining=ctx.retries_remaining,
            replans_used=ctx.replans_used,
            replans_remaining=ctx.replans_remaining,
            decisions_used=ctx.decisions_used,
            decisions_remaining=ctx.decisions_remaining,
        )
        return self._decision_to_update(decision, ctx, state)
