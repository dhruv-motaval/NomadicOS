"""Verification BOUNDARIES (SPEC §27-29; §53 Phase 7 §7.15/§7.16/§7.30).

Phase 8 owns real verification. Phase 7 provides the honest interfaces:

- ``StepVerifier`` protocol + ``NoStepVerifier`` which reports NOT_EVALUATED
  (never "verified") — the graph may continue by plan bookkeeping but may not
  claim step proof.
- ``GoalVerifier`` protocol + ``NoGoalVerifier`` which returns a
  ``VerificationResult`` whose single evidence item is truthful:
  "goal verifier not yet implemented -> observed=False". NOT_PASSED forever.
  SUCCESS is therefore unreachable until Phase 8 supplies a real verifier —
  the boundary cannot be coerced into passing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from nomadicos.contracts.core import Goal, PlanStep
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationResult,
)


class StepOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    NOT_EVALUATED = "NOT_EVALUATED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class StepVerifier(Protocol):
    def verify(
        self, goal: Goal, step: PlanStep, execution: ExecutionResult | None
    ) -> VerificationResult: ...


class GoalVerifier(Protocol):
    def verify(self, goal: Goal) -> VerificationResult: ...

    def verify_task(self, goal: Goal, executions: list[ExecutionResult]) -> VerificationResult: ...


class NoStepVerifier:
    """Phase 7 placeholder boundary: verification absent, honesty present."""

    def verify(
        self, goal: Goal, step: PlanStep, execution: ExecutionResult | None
    ) -> VerificationResult:
        return VerificationResult(
            level=VerificationLevel.STEP,
            task_id=goal.id,
            step_id=step.id,
            verifier="no-step-verifier-placeholder-v0",
            evidence=[
                EvidenceItem(
                    claim="step verification service present",
                    observed=False,
                    detail={"note": "Phase 8 owns step verification (SPEC §7.15)"},
                    unverifiable=True,
                )
            ],
        )

    @staticmethod
    def outcome(result: VerificationResult) -> StepOutcome:
        if result.passed:
            return StepOutcome.VERIFIED
        missing = "verification service present" in " ".join(result.missing)
        return StepOutcome.NOT_EVALUATED if missing else StepOutcome.FAILED


class NoGoalVerifier:
    """Cannot be talked into SUCCESS — there is literally no observed proof."""

    def verify(self, goal: Goal) -> VerificationResult:
        return self.verify_task(goal, [])

    def verify_task(self, goal: Goal, executions: list[ExecutionResult]) -> VerificationResult:
        return VerificationResult(
            level=VerificationLevel.GOAL,
            task_id=goal.id,
            verifier="no-goal-verifier-placeholder-v0",
            evidence=[
                EvidenceItem(
                    claim="independent goal verification exists",
                    observed=False,
                    unverifiable=True,
                    detail={
                        "executions_seen": len(executions),
                        "execution_successes": sum(1 for e in executions if e.succeeded),
                        "note": "goal verification lands in Phase 8; SUCCESS must wait (SPEC §28)",
                    },
                )
            ],
        )
