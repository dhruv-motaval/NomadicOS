"""Task budgets (BP §52, §72): enforced outside the model (I10)."""

import time
from dataclasses import dataclass, field

from nomadicos.core.errors import BudgetExceeded


@dataclass(frozen=True, slots=True)
class TaskBudget:
    max_duration_seconds: float = 600.0
    max_steps: int = 50
    max_retries: int = 3
    max_model_calls: int = 100
    max_tool_calls: int = 200


@dataclass
class TaskBudgetTracker:
    budget: TaskBudget = field(default_factory=TaskBudget)

    _started: float = field(default_factory=time.monotonic)
    _steps: int = 0
    _retries: int = 0
    _model_calls: int = 0
    _tool_calls: int = 0

    def check_step(self) -> None:
        self._steps += 1
        if self._steps > self.budget.max_steps:
            raise BudgetExceeded(
                f"max_steps exceeded ({self.budget.max_steps})",
                context={"steps": self._steps},
            )
        self.check_duration()

    def check_retry(self) -> None:
        self._retries += 1
        if self._retries > self.budget.max_retries:
            raise BudgetExceeded(
                f"max_retries exceeded ({self.budget.max_retries})",
                context={"retries": self._retries},
            )

    def check_model_call(self) -> None:
        self._model_calls += 1
        if self._model_calls > self.budget.max_model_calls:
            raise BudgetExceeded(
                f"max_model_calls exceeded ({self.budget.max_model_calls})",
                context={"model_calls": self._model_calls},
            )

    def check_tool_call(self) -> None:
        self._tool_calls += 1
        if self._tool_calls > self.budget.max_tool_calls:
            raise BudgetExceeded(
                f"max_tool_calls exceeded ({self.budget.max_tool_calls})",
                context={"tool_calls": self._tool_calls},
            )

    def check_duration(self) -> None:
        elapsed = time.monotonic() - self._started
        if elapsed > self.budget.max_duration_seconds:
            raise BudgetExceeded(
                f"max_duration exceeded ({self.budget.max_duration_seconds}s)",
                context={"elapsed_seconds": round(elapsed, 1)},
            )

    def snapshot(self) -> dict[str, int | float]:
        return {
            "elapsed_seconds": round(time.monotonic() - self._started, 1),
            "steps": self._steps,
            "retries": self._retries,
            "model_calls": self._model_calls,
            "tool_calls": self._tool_calls,
        }


__all__ = ["TaskBudget", "TaskBudgetTracker"]
