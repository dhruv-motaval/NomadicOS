"""Benchmark regression gate (Sprint 10): deterministic production
qualification using the EXISTING measured-evidence pattern (Sprint 7/8).

    component benchmarks → like-for-like comparison → verdict

Verdicts are deterministic and evidence-honest:
- INSUFFICIENT_EVIDENCE: fewer than ``min_samples`` like-for-like cases —
  a benchmark with insufficient evidence never automatically passes;
- REGRESSION: any CRITICAL case regressed, or the like-for-like pass rate
  dropped by more than ``max_regression``;
- PASS: otherwise.

No invented production metrics, no silent downgrades, explicit results.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract

DEFAULT_MIN_SAMPLES = 3
DEFAULT_MAX_REGRESSION = 0.1
DEFAULT_MIN_PASS_RATE = 0.8


class QualificationCase(Contract):
    """One recorded benchmark case outcome for one component."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    score: float = Field(ge=0.0, le=1.0)
    critical: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> QualificationCase:
        if not self.case_id.strip():
            raise ValueError("qualification case id must not be empty")
        return self


class QualificationVerdict(StrEnum):
    PASS = "PASS"
    REGRESSION = "REGRESSION"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class QualificationResult(Contract):
    """Deterministic regression-gate verdict (auditable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str
    cases_evaluated: int = Field(ge=0)
    baseline_rate: float | None = None
    current_rate: float | None = None
    delta: float | None = None
    critical_regressions: list[str] = Field(default_factory=list)
    verdict: QualificationVerdict
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> QualificationResult:
        if len(self.reasons) > 8:
            raise ValueError("qualification reasons exceed 8")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("qualification reasons must not be empty")
        return self


class QualificationGate:
    """Deterministic benchmark regression gate (fail-closed)."""

    def __init__(
        self,
        *,
        min_samples: int = 3,
        max_regression: float = 0.1,
        min_pass_rate: float = 0.8,
    ) -> None:
        if min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        if not 0.0 <= max_regression <= 1.0:
            raise ValueError("max_regression must be within [0, 1]")
        if not 0.0 <= min_pass_rate <= 1.0:
            raise ValueError("min_pass_rate must be within [0, 1]")
        self._min_samples = min_samples
        self._max_regression = max_regression
        self._min_pass_rate = min_pass_rate

    def evaluate(
        self,
        component: str,
        baseline: list[QualificationCase],
        current: list[QualificationCase],
    ) -> QualificationResult:
        """Like-for-like deterministic comparison (same case ids only).

        - fewer than ``min_samples`` shared cases → INSUFFICIENT_EVIDENCE
          (a benchmark with insufficient evidence must not auto-pass);
        - any critical case regressed → REGRESSION (critical-capability
          protection);
        - current pass rate below ``min_pass_rate`` → REGRESSION;
        - otherwise PASS."""
        try:
            return self._evaluate(component, baseline, current)
        except Exception as exc:  # fail closed on any comparison error
            return QualificationResult(
                component=component,
                cases_evaluated=0,
                verdict=QualificationVerdict.INSUFFICIENT_EVIDENCE,
                reasons=[f"qualification gate failed closed: {type(exc).__name__}"],
            )

    def _evaluate(
        self,
        component: str,
        baseline: list[QualificationCase],
        current: list[QualificationCase],
    ) -> QualificationResult:
        base_by_id: dict[str, float] = {}
        for case in baseline:
            base_by_id.setdefault(case.case_id, case.score)
        compared: list[tuple[str, float, float, bool]] = []
        for case in current:
            if case.case_id in base_by_id:
                compared.append((case.case_id, base_by_id[case.case_id], case.score, case.critical))
        total = len(compared)
        baseline_rate = (
            round(sum(1 for _, b, _c, _x in compared if b >= self._min_pass_rate) / total, 4)
            if total
            else None
        )
        current_rate = (
            round(sum(1 for _, _b, c, _x in compared if c >= self._min_pass_rate) / total, 4)
            if total
            else None
        )
        critical_regressions = sorted(
            case_id
            for case_id, base_score, cur_score, critical in compared
            if critical and cur_score + self._max_regression < base_score
        )
        delta = (
            round(current_rate - baseline_rate, 4)
            if current_rate is not None and baseline_rate is not None
            else None
        )
        critical_regressions = sorted(
            case_id
            for case_id, base_score, cur_score, critical in compared
            if critical and cur_score + self._max_regression < base_score
        )
        delta = (
            round(current_rate - baseline_rate, 4)
            if current_rate is not None and baseline_rate is not None
            else None
        )
        if total < self._min_samples:
            return QualificationResult(
                component=component,
                cases_evaluated=total,
                baseline_rate=baseline_rate,
                current_rate=current_rate,
                delta=delta,
                critical_regressions=critical_regressions,
                verdict=QualificationVerdict.INSUFFICIENT_EVIDENCE,
                reasons=[
                    f"insufficient evidence: {total} like-for-like cases "
                    f"< {self._min_samples}; no automatic pass without evidence"
                ],
            )
        # At this point total >= min_samples > 0, so rates are not None
        assert baseline_rate is not None
        assert current_rate is not None
        reasons = [
            f"like-for-like pass rate {current_rate:.4f} "
            f"(baseline {baseline_rate:.4f}, delta {delta:+.4f})"
        ]
        if critical_regressions:
            verdict = QualificationVerdict.REGRESSION
            reasons.append(f"critical regression on {critical_regressions}; promotion blocked")
        elif current_rate < baseline_rate - self._max_regression:
            verdict = QualificationVerdict.REGRESSION
            reasons.append(f"pass rate regressed more than {self._max_regression:.2f}; gate failed")
        else:
            verdict = QualificationVerdict.PASS
        return QualificationResult(
            component=component,
            cases_evaluated=total,
            baseline_rate=baseline_rate,
            current_rate=current_rate,
            delta=delta,
            critical_regressions=critical_regressions,
            verdict=verdict,
            reasons=reasons[:8],
        )
