"""Benchmark / evaluation records (SPEC §52A)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from nomadicos.contracts.core import Contract, Goal, parse_predicate
from nomadicos.kernel.config import EngineName

BenchmarkCategory = Literal[
    "bug_fix",
    "feature_implementation",
    "debugging",
    "refactoring",
    "test_generation",
    "dependency_change",
    "multi_file_change",
    "repair",
    "code_review",
    "performance",
]


class BenchmarkTask(Contract):
    """A standardized task. All candidate models see the identical
    environment (benchmark fairness, SPEC §52A)."""

    id: str
    category: BenchmarkCategory
    objective: str
    language: str = "python"
    framework: str = ""
    difficulty: float = Field(ge=0.0, le=1.0)
    workspace_files: dict[str, str] = Field(default_factory=dict)
    setup_commands: list[str] = Field(default_factory=list)
    #: command that must exit 0 for goal "tests_passed"
    test_command: str = "pytest -q"
    goal_predicates: list[Any] = Field(default_factory=list)
    max_steps: int = Field(default=20, ge=1)
    time_budget_s: float = Field(default=600.0, gt=0)

    def goal(self) -> Goal:
        preds = self.goal_predicates or [{"type": "tests_pass", "command": self.test_command}]
        return Goal(
            objective=self.objective,
            predicates=[parse_predicate(p) for p in preds],
        )


class BenchmarkRecord(Contract):
    """Raw measurements, reproducible metadata, immutability (§52A/§52C)."""

    id: str
    model_id: str
    engine_id: EngineName
    benchmark_version: str
    task_id: str
    task_category: BenchmarkCategory
    language: str
    framework: str = ""
    difficulty: float
    success: bool
    goal_verified: bool | None = None
    tests_passed: bool | None = None
    repair_success: bool | None = None
    tool_success: bool | None = None
    verification_compliance: bool | None = None
    time_to_first_token_ms: float | None = None
    total_latency_ms: float | None = None
    tokens_generated: int | None = None
    tokens_per_second: float | None = None
    steps: int | None = None
    retries: int | None = None
    failure_type: str | None = None
    resource_usage: dict[str, float] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkerCriticIterationRecord(Contract):
    """Worker/critic loop measurability (§52D)."""

    id: str
    task_id: str
    iteration: int
    worker_model: str
    critic_model: str
    pre_score: float | None = None
    post_score: float | None = None
    issues_found: int = 0
    issues_fixed: int = 0
    tests_before: str | None = None
    tests_after: str | None = None
    goal_before: bool | None = None
    goal_after: bool | None = None
    latency_s: float | None = None
