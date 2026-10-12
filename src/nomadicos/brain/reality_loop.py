"""Reality loop bridge (Sprint 4 — connecting brain deliberation to real execution).

Bounded recovery implemented:
- max_recovery_attempts enforced in Python (model cannot change)
- Only recoverable failures (not VALIDATION, not AUTHORIZATION) are retried
- Each retry follows: validation → authority → executor → observation → verification
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
from nomadicos.authority.authorization import AuthorizationService, OwnerConflictRequest
from nomadicos.brain.reality import (
    ExecutionAttempt,
    ExecutionAttemptStatus,
    Failure,
    FailureCategory,
    RealityObservation,
    RealityOutcome,
    RecoveryPlan,
)
from nomadicos.contracts.action import ActionProposal
from nomadicos.contracts.core import Goal
from nomadicos.executor.dispatch import Executor
from nomadicos.tools.context import ExecutionContext
from nomadicos.verification.goal import PredicateGoalVerifier


class RealityLoopConfig:
    def __init__(self, max_recovery_attempts: int = 2) -> None:
        if max_recovery_attempts < 0:
            raise ValueError("max_recovery_attempts must be >= 0")
        self.max_recovery_attempts = max_recovery_attempts


class RealityLoop:
    def __init__(
        self,
        validator: ProposalValidator,
        authorizer: AuthorizationService,
        executor: Executor,
        verifier: PredicateGoalVerifier,
        config: RealityLoopConfig | None = None,
    ) -> None:
        self._validator = validator
        self._authorizer = authorizer
        self._executor = executor
        self._verifier = verifier
        self._config = config or RealityLoopConfig()

    async def execute_candidate(
        self,
        action_candidate: dict[str, Any],
        task_id: str,
        step_id: str,
        model_id: str,
        goal: Goal,
        attempt_number: int = 1,
        workspace_root: str | Path = ".",
    ) -> RealityOutcome:
        return await self._execute_with_recovery(
            action_candidate=action_candidate,
            task_id=task_id,
            step_id=step_id,
            model_id=model_id,
            goal=goal,
            attempt_number=attempt_number,
            workspace_root=workspace_root,
            recovery_count=0,
            original_action_candidate_id=action_candidate.get("id", ""),
        )

    async def _execute_with_recovery(
        self,
        action_candidate: dict[str, Any],
        task_id: str,
        step_id: str,
        model_id: str,
        goal: Goal,
        attempt_number: int,
        workspace_root: str | Path,
        recovery_count: int,
        original_action_candidate_id: str,
    ) -> RealityOutcome:
        capability_str = action_candidate.get("capability", "")
        operation = capability_str.split(".", 1)[-1] if capability_str else ""
        proposal = ActionProposal(
            task_id=task_id,
            step_id=step_id,
            model_id=model_id,
            attempt=attempt_number,
            tool=action_candidate.get("tool", ""),
            operation=operation,
            args=action_candidate.get("arguments", {}),
        )

        execution_attempt = ExecutionAttempt(
            action_candidate_id=action_candidate.get("id", ""),
            task_id=task_id,
            step_id=step_id,
            attempt_number=attempt_number,
            tool=proposal.tool,
            operation=proposal.operation,
            args_hash="",
        )

        try:
            capability_ref = self._validator.validate(proposal, ValidationContext(
                task_id=task_id,
                step_id=step_id,
                model_id=model_id,
                attempt=attempt_number,
            ))
            execution_attempt = ExecutionAttempt(
                action_candidate_id=action_candidate.get("id", ""),
                task_id=task_id,
                step_id=step_id,
                attempt_number=attempt_number,
                tool=proposal.tool,
                operation=proposal.operation,
                args_hash="",
                status=ExecutionAttemptStatus.SUBMITTED,
            )
        except Exception as exc:
            execution_attempt = ExecutionAttempt(
                action_candidate_id=action_candidate.get("id", ""),
                task_id=task_id,
                step_id=step_id,
                attempt_number=attempt_number,
                tool=proposal.tool,
                operation=proposal.operation,
                args_hash="",
                status=ExecutionAttemptStatus.FAILED,
            )
            failure = Failure(
                execution_attempt_id=execution_attempt.id,
                category=FailureCategory.VALIDATION_FAILURE,
                message=str(exc),
                details={
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                    "recovery_count": recovery_count,
                },
            )
            return self._build_outcome(
                execution_attempt=execution_attempt,
                failure=failure,
                recovered=recovery_count > 0,
            )

        try:
            auth_result = self._authorizer.authorize(proposal, capability_ref)
            if isinstance(auth_result, OwnerConflictRequest):
                execution_attempt = ExecutionAttempt(
                    action_candidate_id=action_candidate.get("id", ""),
                    task_id=task_id,
                    step_id=step_id,
                    attempt_number=attempt_number,
                    tool=proposal.tool,
                    operation=proposal.operation,
                    args_hash="",
                    status=ExecutionAttemptStatus.FAILED,
                )
                failure = Failure(
                    execution_attempt_id=execution_attempt.id,
                    category=FailureCategory.AUTHORIZATION_FAILURE,
                    message="owner conflict pending",
                    details={
                        "task_id": task_id,
                        "step_id": step_id,
                        "attempt_number": attempt_number,
                        "recovery_count": recovery_count,
                    },
                )
                return self._build_outcome(
                    execution_attempt=execution_attempt,
                    failure=failure,
                    recovered=recovery_count > 0,
                )
            authorized = auth_result
        except Exception as exc:
            execution_attempt = ExecutionAttempt(
                action_candidate_id=action_candidate.get("id", ""),
                task_id=task_id,
                step_id=step_id,
                attempt_number=attempt_number,
                tool=proposal.tool,
                operation=proposal.operation,
                args_hash="",
                status=ExecutionAttemptStatus.FAILED,
            )
            failure = Failure(
                execution_attempt_id=execution_attempt.id,
                category=FailureCategory.AUTHORIZATION_FAILURE,
                message=str(exc),
                details={
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                    "recovery_count": recovery_count,
                },
            )
            return self._build_outcome(
                execution_attempt=execution_attempt,
                failure=failure,
                recovered=recovery_count > 0,
            )

        ctx = ExecutionContext.for_task(task_id, workspace_root)

        try:
            execution_result = await self._executor.execute(authorized, ctx)
            execution_attempt = execution_attempt.model_copy(
                update={
                    "status": ExecutionAttemptStatus.COMPLETED,
                    "execution_result": execution_result,
                }
            )
        except Exception as exc:
            execution_attempt = execution_attempt.model_copy(
                update={"status": ExecutionAttemptStatus.ERRORED}
            )
            failure = Failure(
                execution_attempt_id=execution_attempt.id,
                category=FailureCategory.EXECUTION_FAILURE,
                message=str(exc),
                details={
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                    "recovery_count": recovery_count,
                },
            )
            return self._build_outcome(
                execution_attempt=execution_attempt,
                failure=failure,
                recovered=recovery_count > 0,
            )

        observation = RealityObservation(
            id=execution_result.id,
            execution_attempt_id=execution_attempt.id,
            task_id=task_id,
            kind="STEP_RESULT",
            summary=(
                f"{execution_result.tool}."
                f"{execution_result.operation} "
                f"{execution_result.status.value}"
            ),
            data={
                "exit_code": execution_result.exit_code,
                "stdout": execution_result.stdout,
                "stderr": execution_result.stderr,
                "evidence": execution_result.evidence,
                "process_id": execution_result.process_id,
                "message": execution_result.message,
            },
            at=execution_result.finished_at,
        )

        try:
            verification_result = self._verifier.verify_task(goal, [execution_result])
        except Exception as exc:
            failure = Failure(
                execution_attempt_id=execution_attempt.id,
                observation_id=observation.id,
                category=FailureCategory.VERIFICATION_FAILURE,
                message=str(exc),
                details={
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                    "recovery_count": recovery_count,
                },
            )
            return self._build_outcome(
                execution_attempt=execution_attempt,
                observation=observation,
                failure=failure,
                recovered=recovery_count > 0,
            )

        outcome = self._build_outcome(
            execution_attempt=execution_attempt,
            observation=observation,
            verification_result=verification_result,
            recovered=recovery_count > 0,
        )

        if not outcome.succeeded:
            failure = Failure(
                execution_attempt_id=execution_attempt.id,
                observation_id=observation.id if observation else None,
                verification_result_id=verification_result.id if verification_result else None,
                category=FailureCategory.VERIFICATION_FAILURE,
                message=(
                    f"verification outcome: {verification_result.outcome.value}"
                    if verification_result and verification_result.outcome
                    else "verification failed"
                ),
                details={
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                    "recovery_count": recovery_count,
                },
            )
            outcome = outcome.model_copy(update={"failure": failure})
            recovery_plan = RecoveryPlan(
                failure_id=failure.id,
                task_id=task_id,
                original_action_candidate_id=original_action_candidate_id,
                retry_count=recovery_count,
                max_retries=self._config.max_recovery_attempts,
            )

            if recovery_count < self._config.max_recovery_attempts:
                return await self._execute_with_recovery(
                    action_candidate=action_candidate,
                    task_id=task_id,
                    step_id=step_id,
                    model_id=model_id,
                    goal=goal,
                    attempt_number=attempt_number + 1,
                    workspace_root=workspace_root,
                    recovery_count=recovery_count + 1,
                    original_action_candidate_id=original_action_candidate_id,
                )

            return outcome.model_copy(update={"recovery_plan": recovery_plan})

        return outcome

    def _build_outcome(
        self,
        execution_attempt: ExecutionAttempt,
        observation: RealityObservation | None = None,
        verification_result: Any = None,
        failure: Failure | None = None,
        recovered: bool = False,
    ) -> RealityOutcome:
        return RealityOutcome(
            id=execution_attempt.id,
            action_candidate_id=execution_attempt.action_candidate_id,
            task_id=execution_attempt.task_id,
            step_id=execution_attempt.step_id,
            attempt_number=execution_attempt.attempt_number,
            execution_attempt=execution_attempt,
            observation=observation,
            verification_result=verification_result,
            failure=failure,
            recovered=recovered,
        )

    def _failure_outcome(
        self,
        task_id: str,
        step_id: str,
        attempt_number: int,
        category: FailureCategory,
        message: str,
    ) -> dict[str, Any]:
        return {
            "action_candidate_id": "",
            "task_id": task_id,
            "step_id": step_id,
            "attempt_number": attempt_number,
            "execution_attempt": None,
            "observation": None,
            "verification_result": None,
            "failure": {
                "id": "",
                "category": category,
                "message": message,
                "details": {
                    "task_id": task_id,
                    "step_id": step_id,
                    "attempt_number": attempt_number,
                },
                "occurred_at": datetime.now(UTC),
            },
            "completed_at": datetime.now(UTC),
        }
