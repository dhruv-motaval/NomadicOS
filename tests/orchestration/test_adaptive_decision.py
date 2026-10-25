"""Phase 4D — Adaptive orchestration and goal-verification tests (SPEC §4D).

Tests the AdaptiveDecisionOrchestrator: continuation, bounded retries, bounded
replanning, escalation, stopping, budget enforcement, OUTCOME_UNKNOWN blocking,
authorization denial blocking, and fail-closed validation.
"""

from __future__ import annotations

from typing import Any

from nomadicos.contracts.adaptive_decision import Decision, DecisionCategory
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.kernel.config import BudgetConfig
from nomadicos.orchestration.adaptive_decision import AdaptiveDecisionOrchestrator


def _execution_result(
    succeeded: bool = True, task_id: str = "t0", step_id: str = "s0"
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        step_id=step_id,
        action_fingerprint="fp-" + step_id,
        tool="filesystem",
        operation="write",
        action_id="action-" + step_id,
        model_id="model-1",
        status=ExecutionStatus.SUCCEEDED if succeeded else ExecutionStatus.FAILED,
    )


def _base_state(task_status: str = TaskStatus.RUNNING.value) -> dict[str, Any]:
    return {
        "task_id": "mission-1",
        "goal": {
            "id": "goal-1",
            "objective": "test mission",
            "predicates": [],
        },
        "current_task_id": "t0",
        "plan": [],
        "current_step": 0,
        "model_id": "model-1",
        "proposals": [],
        "executions": [],
        "verifications": [],
        "failures": [],
        "task_status": task_status,
        "task_status_map": {},
        "task_results": {},
        "brain_planner_enabled": True,
        "brain_actor": False,
        "brain_planner": True,
        "mission_plan": [],
        "observations": [],
        "world_facts": {},
        "attempt": 1,
        "last_failure": None,
        "tried_models": [],
    }


# --------------------------------------------------------------------------- #
# 1. CONTINUE — normal execution, all tasks done
# ---------------------------------------------------------------------------


def test_adaptive_continue_all_tasks_done() -> None:
    """When all tasks are completed, CONTINUE with no change."""
    state = _base_state()
    state["task_status"] = TaskStatus.SUCCESS.value
    state["task_status_map"] = {"t0": TaskStatus.SUCCESS.value}
    state["mission_plan"] = [{"id": "t0", "status": TaskStatus.SUCCESS.value}]

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.CONTINUE.value


# --------------------------------------------------------------------------- #
# 2. RETRY — transient failure with budget remaining
# ---------------------------------------------------------------------------


def test_adaptive_retry_transient_failure() -> None:
    """When last execution failed and retries remain, RETRY."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["executions"] = [_execution_result(succeeded=False, step_id="s0")]
    state["last_failure"] = {"category": "ACTION_FAILED", "message": "timeout"}
    state["task_status_map"] = {"t0": TaskStatus.RUNNING.value}

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_retries_per_task=3))
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.RETRY.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.TASK_FAILED.value


# --------------------------------------------------------------------------- #
# 3. RETRY budget exhausted -> STOP
# ---------------------------------------------------------------------------


def test_adaptive_retry_budget_exhausted_stop() -> None:
    """When the receiving task's trusted retry counter is exhausted, STOP."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["executions"] = [_execution_result(succeeded=False, step_id="s0")]
    state["last_failure"] = {"category": "ACTION_FAILED", "message": "timeout"}
    state["task_status_map"] = {"t0": TaskStatus.RUNNING.value}
    state["task_retries"] = {"t0": 3}

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_retries_per_task=3))
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.BUDGET_EXHAUSTED.value
    assert result["task_status"] == TaskStatus.BLOCKED.value


# --------------------------------------------------------------------------- #
# 4. REPLAN — goal not satisfied, replan budget remaining
# ---------------------------------------------------------------------------


def test_adaptive_replan_goal_not_satisfied() -> None:
    """When goal not satisfied and replans remain, REPLAN."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["executions"] = [_execution_result(succeeded=True, step_id="s0")]
    state["verifications"] = [
        VerificationResult(
            level=VerificationLevel.GOAL,
            task_id="task_1",
            step_id="s0",
            outcome=VerificationOutcome.NOT_PASS,
            verifier="goal_verifier",
            evidence=[EvidenceItem(claim="goal requirement", observed=False)],
        )
    ]
    state["task_status_map"] = {"t0": TaskStatus.SUCCESS.value}

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_replans_per_mission=3))
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.REPLAN.value


# --------------------------------------------------------------------------- #
# 5. REPLAN budget exhausted -> STOP
# ---------------------------------------------------------------------------


def test_adaptive_replan_budget_exhausted_stop() -> None:
    """When replan budget exhausted, STOP."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["executions"] = [_execution_result(succeeded=True, step_id="s0")]
    state["verifications"] = [
        VerificationResult(
            level=VerificationLevel.GOAL,
            task_id="task_1",
            step_id="s0",
            outcome=VerificationOutcome.NOT_PASS,
            verifier="goal_verifier",
            evidence=[EvidenceItem(claim="goal requirement", observed=False)],
        )
    ]
    state["task_status_map"] = {"t0": TaskStatus.SUCCESS.value}
    state["replans_used"] = 3

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_replans_per_mission=3))
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.BUDGET_EXHAUSTED.value


# --------------------------------------------------------------------------- #
# 6. ESCALATE — model error or resource unavailable
# ---------------------------------------------------------------------------


def test_adaptive_escalate_model_error() -> None:
    """When model error category, ESCALATE."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["last_failure"] = {"category": "MODEL_ERROR", "message": "context window exceeded"}

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.ESCALATE.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.MODEL_ERROR.value


# --------------------------------------------------------------------------- #
# 7. STOP — AUTHORIZATION_DENIED cannot be retried
# ---------------------------------------------------------------------------


def test_adaptive_authorization_denied_stop() -> None:
    """When authorization denied, STOP — never retry."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["last_failure"] = {"category": "AUTHORIZATION_DENIED", "message": "forbidden"}

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.AUTHORIZATION_DENIED.value


# --------------------------------------------------------------------------- #
# 8. OUTCOME_UNKNOWN blocks retry — never blind replay
# ---------------------------------------------------------------------------


def test_adaptive_outcome_unknown_blocks() -> None:
    """When outcome_uncertain, STOP regardless of other conditions."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["outcome_uncertain"] = True

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.OUTCOME_UNKNOWN.value


# --------------------------------------------------------------------------- #
# 9. Decision budget exhausted -> STOP
# ---------------------------------------------------------------------------


def test_adaptive_decision_budget_exhausted_stop() -> None:
    """When decision budget exhausted, STOP."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["adaptive_decisions_used"] = 10

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_adaptive_decisions_per_mission=10))
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["adaptive_decision"]["category"] == DecisionCategory.BUDGET_EXHAUSTED.value


# --------------------------------------------------------------------------- #
# 10. Reasoning budget: when exhausted, fall back to deterministic rules
# ---------------------------------------------------------------------------


def test_adaptive_reasoning_budget_exhausted_falls_back() -> None:
    """When reasoning budget exhausted, deterministic rules still apply."""
    state = _base_state()
    state["task_status"] = TaskStatus.RUNNING.value
    state["executions"] = [_execution_result(succeeded=False, step_id="s0")]
    state["last_failure"] = {"category": "ACTION_FAILED", "message": "timeout"}
    state["task_status_map"] = {"t0": TaskStatus.RUNNING.value}
    state["adaptive_decisions_used"] = 2  # at budget

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig(max_adaptive_decisions_per_mission=2))
    result = app.decide(state)

    # Should still make a decision (RETRY based on deterministic rules)
    assert result["adaptive_decision"]["decision"] in (Decision.RETRY.value, Decision.STOP.value)


# --------------------------------------------------------------------------- #
# 11. Invalid decomposition detected
# ---------------------------------------------------------------------------


def test_adaptive_invalid_decomposition_stop() -> None:
    """When decomposition is malformed (missing goal), STOP."""
    state = _base_state()
    state["goal"] = {}  # missing objective — invalid decomposition
    state["task_status"] = TaskStatus.RUNNING.value

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value


# --------------------------------------------------------------------------- #
# 12. Previous decisions preserved
# ---------------------------------------------------------------------------


def test_adaptive_previous_decisions_preserved() -> None:
    """Previous decisions are carried forward in context."""
    state = _base_state()
    state["adaptive_decisions_used"] = 2

    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide(state)

    # Previous decisions should be reflected in context
    assert result["adaptive_decisions_used"] >= 2


# --------------------------------------------------------------------------- #
# 13. Empty state — fail closed
# ---------------------------------------------------------------------------


def test_adaptive_empty_state_stop() -> None:
    """With empty state, STOP safely."""
    app = AdaptiveDecisionOrchestrator(budget=BudgetConfig())
    result = app.decide({})

    assert result["adaptive_decision"]["decision"] == Decision.STOP.value
    assert result["task_status"] == TaskStatus.BLOCKED.value
