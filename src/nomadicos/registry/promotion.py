"""Replay-based promotion (Sprint 7): a candidate earns the default role
only by beating the INCUMBENT on the SAME replay cases, with bounded
minimum evidence, and without regressing an important case.

Replay cases are reconstructable historical evaluations (task id +
category + difficulty + predicates) — compared like-for-like: only case
ids present for BOTH models count. One successful case never promotes
(``min_cases``); a candidate failure on a case the incumbent succeeded
blocks promotion when the case is CRITICAL (regression guard). Every
decision carries an auditable, deterministic reason trail. Fail closed:
any comparison problem keeps the incumbent. No live model calls —
results arrive as recorded outcomes (from benchmarks, canaries, or
stored production evidence).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import TaskType

DEFAULT_MIN_CASES = 3
DEFAULT_MIN_WIN_RATE = 0.6


class ReplayCase(Contract):
    """One reconstructable historical case (deterministic identity)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    task_type: TaskType = TaskType.REASONING
    difficulty: float = Field(default=0.5, ge=0.0, le=1.0)
    #: important case: candidate regression here blocks promotion
    critical: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> ReplayCase:
        if not self.case_id.strip():
            raise ValueError("replay case id must not be empty")
        return self


class ReplayCaseResult(Contract):
    """One recorded outcome of one model on one replay case. DATA only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str
    case_id: str
    success: bool
    score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> ReplayCaseResult:
        if not self.model_id.strip() or not self.case_id.strip():
            raise ValueError("replay result requires model and case ids")
        return self


class ReplayComparison(Contract):
    """Like-for-like comparison of candidate vs incumbent (audit data)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incumbent_model_id: str
    candidate_model_id: str
    cases_evaluated: int = Field(default=0, ge=0)
    candidate_wins: int = Field(default=0, ge=0)
    incumbent_wins: int = Field(default=0, ge=0)
    ties: int = Field(default=0, ge=0)
    #: case ids where the candidate regressed vs the incumbent
    regressions: list[str] = Field(default_factory=list)
    critical_regressions: list[str] = Field(default_factory=list)
    candidate_win_rate: float | None = None


class ReplayPromotionDecision(StrEnum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


class ReplayPromotionResult(Contract):
    """Auditable promotion verdict (deterministic reason trail)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incumbent_model_id: str
    candidate_model_id: str
    comparison: ReplayComparison
    decision: ReplayPromotionDecision
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> ReplayPromotionResult:
        if len(self.reasons) > 8:
            raise ValueError("promotion reasons exceed 8")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("promotion reasons must not be empty")
        return self


class ReplayPromoter:
    """Deterministic like-for-like promotion decisions. Fail closed."""

    def __init__(
        self,
        *,
        min_cases: int = DEFAULT_MIN_CASES,
        min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    ) -> None:
        if min_cases < 1:
            raise ValueError("min_cases must be >= 1 (one case never promotes)")
        if not 0.0 <= min_win_rate <= 1.0:
            raise ValueError("min_win_rate must be within [0, 1]")
        self._min_cases = min_cases
        self._min_win_rate = min_win_rate

    def evaluate(
        self,
        incumbent_model_id: str,
        candidate_model_id: str,
        incumbent_results: list[ReplayCaseResult],
        candidate_results: list[ReplayCaseResult],
        cases: list[ReplayCase] | None = None,
    ) -> ReplayPromotionResult:
        try:
            return self._evaluate(
                incumbent_model_id, candidate_model_id, incumbent_results, candidate_results, cases
            )
        except Exception as exc:  # fail closed: keep the incumbent
            return ReplayPromotionResult(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                comparison=ReplayComparison(
                    incumbent_model_id=incumbent_model_id,
                    candidate_model_id=candidate_model_id,
                ),
                decision=ReplayPromotionDecision.REJECT,
                reasons=[f"replay promotion failed closed: {type(exc).__name__}"],
            )

    def _evaluate(
        self,
        incumbent_model_id: str,
        candidate_model_id: str,
        incumbent_results: list[ReplayCaseResult],
        candidate_results: list[ReplayCaseResult],
        cases: list[ReplayCase] | None,
    ) -> ReplayPromotionResult:
        if incumbent_model_id == candidate_model_id:
            raise ValueError("replay promotion requires distinct models")
        critical_cases = {c.case_id for c in (cases or []) if c.critical}
        #: incumbent success per case (first record wins — deterministic)
        by_case: dict[str, bool] = {}
        for result in incumbent_results:
            if result.model_id != incumbent_model_id:
                continue  # foreign evidence never blends (deterministic)
            by_case.setdefault(result.case_id, result.success)
        compared: list[tuple[str, bool, bool]] = []
        for result in candidate_results:
            if result.model_id != candidate_model_id:
                continue
            if result.case_id in by_case:
                compared.append((result.case_id, by_case[result.case_id], result.success))
        total = len(compared)
        candidate_wins = sum(1 for _, i, c in compared if c and not i)
        incumbent_wins = sum(1 for _, i, c in compared if i and not c)
        ties = total - candidate_wins - incumbent_wins
        regressions = sorted(case for case, i, c in compared if i and not c)
        critical_regressions = sorted(r for r in regressions if r in critical_cases)
        win_rate = round(candidate_wins / total, 4) if total else None
        comparison = ReplayComparison(
            incumbent_model_id=incumbent_model_id,
            candidate_model_id=candidate_model_id,
            cases_evaluated=total,
            candidate_wins=candidate_wins,
            incumbent_wins=incumbent_wins,
            ties=ties,
            regressions=regressions,
            critical_regressions=critical_regressions,
            candidate_win_rate=win_rate,
        )
        if total < self._min_cases:
            return ReplayPromotionResult(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                comparison=comparison,
                decision=ReplayPromotionDecision.INCONCLUSIVE,
                reasons=[
                    f"insufficient evidence: {total} like-for-like cases < {self._min_cases}; "
                    f"incumbent preserved (one case never promotes)"
                ],
            )
        if critical_regressions:
            return ReplayPromotionResult(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                comparison=comparison,
                decision=ReplayPromotionDecision.REJECT,
                reasons=[
                    f"critical regression on {critical_regressions}; promotion blocked",
                ],
            )
        # incumbent_wins counts exactly the like-for-like regressions:
        # ANY regression blocks promotion (conservative, deterministic)
        if regressions:
            return ReplayPromotionResult(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                comparison=comparison,
                decision=ReplayPromotionDecision.REJECT,
                reasons=[
                    f"regressions {regressions}; promotion blocked, incumbent preserved",
                ],
            )
        if win_rate is not None and win_rate >= self._min_win_rate:
            return ReplayPromotionResult(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                comparison=comparison,
                decision=ReplayPromotionDecision.PROMOTE,
                reasons=[
                    f"candidate win rate {win_rate:.4f} >= {self._min_win_rate:.2f} "
                    f"across {total} like-for-like cases with no incumbent wins",
                    f"no critical regressions; audit trail: {comparison.model_dump(mode='json')}"[
                        :400
                    ],
                ],
            )
        return ReplayPromotionResult(
            incumbent_model_id=incumbent_model_id,
            candidate_model_id=candidate_model_id,
            comparison=comparison,
            decision=ReplayPromotionDecision.REJECT,
            reasons=[
                f"candidate win rate {win_rate} below required {self._min_win_rate:.2f} "
                f"or incumbent still wins cases; incumbent preserved",
            ],
        )


__all__ = [
    "DEFAULT_MIN_CASES",
    "DEFAULT_MIN_WIN_RATE",
    "ReplayCase",
    "ReplayCaseResult",
    "ReplayComparison",
    "ReplayPromotionDecision",
    "ReplayPromotionResult",
    "ReplayPromoter",
]
