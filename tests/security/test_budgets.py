"""Budget enforcement tests (BP §52, §72 — I10: budgets enforced outside the model)."""

import pytest

from nomadicos.core.errors import BudgetExceeded
from nomadicos.security.budgets import TaskBudget, TaskBudgetTracker


def test_step_budget() -> None:
    tracker = TaskBudgetTracker(TaskBudget(max_steps=3))
    for _ in range(3):
        tracker.check_step()
    with pytest.raises(BudgetExceeded, match="max_steps"):
        tracker.check_step()


def test_retry_budget() -> None:
    tracker = TaskBudgetTracker(TaskBudget(max_retries=2))
    tracker.check_retry()
    tracker.check_retry()
    with pytest.raises(BudgetExceeded, match="max_retries"):
        tracker.check_retry()


def test_model_and_tool_call_budgets() -> None:
    tracker = TaskBudgetTracker(TaskBudget(max_model_calls=2, max_tool_calls=1))
    tracker.check_model_call()
    tracker.check_model_call()
    with pytest.raises(BudgetExceeded, match="max_model_calls"):
        tracker.check_model_call()
    tracker.check_tool_call()
    with pytest.raises(BudgetExceeded, match="max_tool_calls"):
        tracker.check_tool_call()


def test_duration_budget() -> None:
    tracker = TaskBudgetTracker(TaskBudget(max_duration_seconds=0.0))
    with pytest.raises(BudgetExceeded, match="max_duration"):
        tracker.check_duration()


def test_snapshot_reports_usage() -> None:
    tracker = TaskBudgetTracker(TaskBudget(max_steps=10))
    tracker.check_step()
    tracker.check_model_call()
    snap = tracker.snapshot()
    assert snap["steps"] == 1
    assert snap["model_calls"] == 1
