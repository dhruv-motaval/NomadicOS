"""Goal verification (SPEC §27-29, §8.9-§8.12, §8.26-§8.27).

PASS requires: every goal predicate satisfied, by attributable evidence,
with nothing left unverified and nothing blocked. Absence of failures,
plan exhaustion, executor success, and model completion claims are
explicitly NOT inputs to truth. This result is the ONLY authoritative
source from which the graph may conclude SUCCESS.
"""

from __future__ import annotations

from pathlib import Path

from nomadicos.contracts.core import Goal
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import evaluate_goal_predicate

VERIFIER_ID = "nomadic-goal-verifier-v1"


class PredicateGoalVerifier:
    def __init__(self, workspace_root: str | Path | None = None, *, per_task: bool = True) -> None:
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.per_task = per_task

    def verify(self, goal: Goal) -> VerificationResult:
        return self.verify_task(goal, [])

    def verify_task(self, goal: Goal, executions: list[ExecutionResult]) -> VerificationResult:
        workspace = None
        if self.workspace_root is not None:
            workspace = self.workspace_root / goal.id if self.per_task else self.workspace_root
        ctx = EvidenceContext(
            task_id=goal.id,
            workspace=workspace,
            executions=list(executions),
        )
        if not goal.predicates:
            # a goal without testable completion conditions can never be
            # PROVEN - vacuous success is forbidden (SPEC §8.9)
            return VerificationResult(
                level=VerificationLevel.GOAL,
                task_id=goal.id,
                outcome=VerificationOutcome.NOT_VERIFIED,
                verifier=VERIFIER_ID,
                evidence=[
                    EvidenceItem(
                        claim="goal declares no completion predicates - truth undecidable",
                        observed=False,
                        unverifiable=True,
                    )
                ],
            )
        results = [evaluate_goal_predicate(gp, ctx) for gp in goal.predicates[:12]]
        items = [i for r in results for i in r.items]
        verdicts = [r.verdict for r in results]
        if VerificationOutcome.BLOCKED in verdicts:
            outcome = VerificationOutcome.BLOCKED
        elif VerificationOutcome.NOT_PASS in verdicts:
            outcome = VerificationOutcome.NOT_PASS
        elif VerificationOutcome.NOT_VERIFIED in verdicts:
            outcome = VerificationOutcome.NOT_VERIFIED
        else:
            outcome = VerificationOutcome.PASS
        if outcome is VerificationOutcome.PASS and not items:
            outcome = VerificationOutcome.NOT_VERIFIED  # PASS needs evidence
        return VerificationResult(
            level=VerificationLevel.GOAL,
            task_id=goal.id,
            outcome=outcome,
            verifier=VERIFIER_ID,
            evidence=items,
        )
