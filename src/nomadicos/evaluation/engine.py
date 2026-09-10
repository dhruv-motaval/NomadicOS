"""EvaluationEngine: verify + score task runs (BP §100, §137 state machine).

evaluate.verify(task_run) → verdict; evaluate.score(task_run) → record.
Scores feed experience records (Phase 11) and model selection statistics
(BP §64, §148).
"""

import time

from pydantic import BaseModel, ConfigDict, Field

from nomadicos.core.errors import VerificationFailed
from nomadicos.core.logging import get_logger
from nomadicos.evaluation.base import EvaluationVerdict, Evidence, Verifier

logger = get_logger("evaluation.engine")


class RunRecord(BaseModel):
    """One completed task-run evaluation (BP §64, §206)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    run_id: str
    verified: bool
    verdict_summary: str
    checks_passed: int
    checks_total: int
    steps_taken: int
    retries_used: int
    duration_seconds: float
    completed_at_monotonic: float = Field(default_factory=time.monotonic)

    @property
    def score(self) -> float:
        """0-10: verification dominates (BP §216: new success rate must beat old)."""
        if self.checks_total == 0:
            return 0.0
        base = (self.checks_passed / self.checks_total) * 10.0
        if not self.verified:
            base = min(base, 4.0)  # unverified runs can never score high
        return round(base, 2)


class EvaluationEngine:
    def __init__(self, verifiers: dict[str, Verifier] | None = None) -> None:
        # kind → verifier (BP §144: different tools need different verification)
        self._verifiers: dict[str, Verifier] = verifiers or {
            "filesystem": __import__(
                "nomadicos.evaluation.deterministic", fromlist=["FilesystemVerifier"]
            ).FilesystemVerifier(),
            "terminal": __import__(
                "nomadicos.evaluation.deterministic", fromlist=["TerminalVerifier"]
            ).TerminalVerifier(),
        }

    def register_verifier(self, kind: str, verifier: Verifier) -> None:
        self._verifiers[kind] = verifier

    async def verify(self, kind: str, evidence: Evidence) -> EvaluationVerdict:
        """Verify one evidence bundle against its kind's verifier (BP §100)."""
        verifier = self._verifiers.get(kind)
        if verifier is None:
            raise VerificationFailed(
                f"no verifier registered for evidence kind: {kind}",
                context={"available": sorted(self._verifiers)},
            )
        return await verifier.verify(evidence)

    async def evaluate_run(
        self,
        *,
        task_id: str,
        run_id: str,
        evidence_bundles: list[tuple[str, Evidence]],
        steps_taken: int = 0,
        retries_used: int = 0,
        duration_seconds: float = 0.0,
    ) -> RunRecord:
        """Verify all evidence bundles and produce one scored record (BP §100)."""
        total_passed = 0
        total_checks = 0
        all_verified = True
        summaries: list[str] = []

        for kind, evidence in evidence_bundles:
            verdict = await self.verify(kind, evidence)
            total_passed += sum(1 for c in verdict.checks if c.passed)
            total_checks += len(verdict.checks)
            all_verified = all_verified and verdict.verified
            summaries.append(verdict.summary)

        record = RunRecord(
            task_id=task_id,
            run_id=run_id,
            verified=all_verified,
            verdict_summary="; ".join(summaries) or "no evidence collected",
            checks_passed=total_passed,
            checks_total=total_checks,
            steps_taken=steps_taken,
            retries_used=retries_used,
            duration_seconds=duration_seconds,
        )
        logger.info(
            "run evaluated task=%s run=%s verified=%s score=%s",
            task_id,
            run_id,
            record.verified,
            record.score,
        )
        return record


__all__ = ["EvaluationEngine", "RunRecord"]
