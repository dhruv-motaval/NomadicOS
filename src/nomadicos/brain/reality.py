"""Reality loop contracts (Sprint 4 — connecting deliberation to real execution).

The reality loop is the bridge between brain deliberation and the runtime:

    ActionCandidate/Decision → validation → authority → executor
        → Observation → verifier → VerificationResult

Strict boundaries maintained:

- Prediction: What we EXPECT to happen (WorldState hypothesis)
- Observation: What ACTUALLY happened (real ExecutionResult)
- Verification: Did the intended result ACTUALLY occur? (independent)

Model output NEVER directly creates side effects. The executor consumes
ONLY AuthorizedAction (SPEC §56.7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from nomadicos.contracts.core import Contract
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import VerificationResult
from nomadicos.kernel.ids import new_id


class ExecutionAttemptStatus(StrEnum):
    """Execution attempt lifecycle status."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ERRORED = "ERRORED"


class ExecutionAttempt(Contract):
    """One attempt to execute a selected action.

    Repeated attempts for the same ActionCandidate create separate
    ExecutionAttempt records (replayable history).
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("exec"))
    action_candidate_id: str
    task_id: str
    step_id: str
    attempt_number: int
    tool: str
    operation: str
    args_hash: str
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ExecutionAttemptStatus = ExecutionAttemptStatus.PENDING
    execution_result: ExecutionResult | None = None


class RealityObservation(Contract):
    """Observed world state after execution (reality loop contract).

    This is DATA, not authority. It may contradict predictions.
    Uses RuntimeObservation fields but wrapped for reality loop tracking.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("obs"))
    execution_attempt_id: str
    task_id: str
    kind: str
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ObservationKind(StrEnum):
    TERMINAL = "TERMINAL"
    FILESYSTEM = "FILESYSTEM"
    PROCESS = "PROCESS"
    DESKTOP = "DESKTOP"
    STEP_RESULT = "STEP_RESULT"
    NOTE = "NOTE"


class FailureCategory(StrEnum):
    """Deterministic failure classification (SPEC §30)."""
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    AUTHORIZATION_FAILURE = "AUTHORIZATION_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    OBSERVATION_FAILURE = "OBSERVATION_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    UNKNOWN = "UNKNOWN"


class Failure(Contract):
    """A classified failure with deterministic category.

    Never inferred from model output. Always produced by deterministic
    classification of actual runtime outcomes.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("rel"))
    execution_attempt_id: str | None = None
    observation_id: str | None = None
    verification_result_id: str | None = None
    category: FailureCategory
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_retryable(self) -> bool:
        """Determines if this failure category allows retry."""
        return self.category not in (
            FailureCategory.VALIDATION_FAILURE,
            FailureCategory.AUTHORIZATION_FAILURE,
        )


class RecoveryPlan(Contract):
    """Structured recovery plan for a failed execution.

    Bounded retry limit enforced by orchestrator; model cannot increase it.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("rec"))
    failure_id: str
    task_id: str
    original_action_candidate_id: str
    new_action_candidates: list[str] = Field(default_factory=list)
    retry_count: int = 0
    max_retries: int = 3
    planned_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RealityOutcome(Contract):
    """Final outcome of the reality loop for one selected action.

    Carries the complete trace: execution attempt, observation, verification.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("reality"))
    action_candidate_id: str
    task_id: str
    step_id: str
    attempt_number: int
    execution_attempt: ExecutionAttempt | None = None
    observation: RealityObservation | None = None
    verification_result: VerificationResult | None = None
    failure: Failure | None = None
    recovered: bool = False
    recovery_plan: RecoveryPlan | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def succeeded(self) -> bool:
        """True only if verification passed."""
        if self.verification_result is None:
            return False
        return self.verification_result.outcome is not None and \
            self.verification_result.outcome.value == "PASS"


def classify_failure(
    status: ExecutionAttemptStatus,
    execution_result: ExecutionResult | None,
    verification_result: VerificationResult | None,
) -> FailureCategory:
    """Deterministic failure classification — NEVER model-driven.

    Returns the first matching category in priority order.
    """
    if execution_result is None:
        if status in (ExecutionAttemptStatus.TIMEOUT,):
            return FailureCategory.TIMEOUT
        return FailureCategory.UNKNOWN

    if status is ExecutionAttemptStatus.TIMEOUT:
        return FailureCategory.TIMEOUT

    if status is ExecutionAttemptStatus.ERRORED:
        return FailureCategory.EXECUTION_FAILURE

    if status is ExecutionAttemptStatus.COMPLETED:
        if verification_result is not None and not verification_result.passed:
            return FailureCategory.VERIFICATION_FAILURE
        return FailureCategory.EXECUTION_FAILURE

    return FailureCategory.UNKNOWN
