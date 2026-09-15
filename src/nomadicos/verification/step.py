"""Step verification (SPEC §27, §8.3, §8.8, §8.16).

"Did the expected result of THIS step actually occur?" - answered by
evaluating the step's owned completion predicate against correlated,
real evidence. An executor SUCCEEDED status is NOT itself verification;
a step without an explicit expectation yields NOT_VERIFIED (informational)
so Phase 7 routing can proceed on plan bookkeeping without any node
claiming proof it does not have.
"""

from __future__ import annotations

from pathlib import Path

from nomadicos.contracts.core import Goal, PlanStep
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import evaluate_goal_predicate

VERIFIER_ID = "nomadic-step-verifier-v1"


class PredicateStepVerifier:
    def __init__(self, workspace_root: str | Path | None = None, *, per_task: bool = True) -> None:
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.per_task = per_task

    def verify(
        self,
        goal: Goal,
        step: PlanStep,
        execution: ExecutionResult | None,
        executions: list[ExecutionResult] | None = None,
    ) -> VerificationResult:
        pool: list[ExecutionResult] = list(executions or [])
        if execution is not None:
            pool = pool + [execution]
        workspace = None
        if self.workspace_root is not None:
            workspace = self.workspace_root / goal.id if self.per_task else self.workspace_root
        ctx = EvidenceContext(
            task_id=goal.id,
            workspace=workspace,
            executions=pool,
        )
        if step.expected is None:
            return VerificationResult(
                level=VerificationLevel.STEP,
                task_id=goal.id,
                step_id=step.id,
                verifier=VERIFIER_ID,
                evidence=[
                    EvidenceItem(
                        claim="step defines no explicit completion predicate - "
                        "executed, but not proof of any expectation (SPEC §8.16)",
                        observed=False,
                        unverifiable=True,
                    )
                ],
            )
        result = evaluate_goal_predicate(step.expected, ctx)
        items: list[EvidenceItem] = list(result.items)
        # correlation guard: an execution attached here must belong to this task
        if execution is not None and execution.task_id != goal.id:
            items.append(
                EvidenceItem(
                    claim="attached execution evidence belongs to another task - ignored",
                    observed=False,
                    unverifiable=True,
                    detail={"foreign_task": execution.task_id},
                )
            )
            if result.verdict is VerificationOutcome.PASS:
                result.verdict = VerificationOutcome.NOT_PASS
        return VerificationResult(
            level=VerificationLevel.STEP,
            task_id=goal.id,
            step_id=step.id,
            outcome=result.verdict,
            verifier=VERIFIER_ID,
            evidence=items,
        )
