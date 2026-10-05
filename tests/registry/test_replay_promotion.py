"""Sprint 7 — replay-based promotion: like-for-like case comparison,
minimum evidence, regression blocking, deterministic auditable verdicts."""

from __future__ import annotations

from nomadicos.registry.promotion import (
    ReplayCase,
    ReplayCaseResult,
    ReplayPromoter,
    ReplayPromotionDecision,
)


def results(model_id: str, outcomes: dict[str, bool]) -> list[ReplayCaseResult]:
    return [
        ReplayCaseResult(model_id=model_id, case_id=case_id, success=success)
        for case_id, success in outcomes.items()
    ]


def make_cases(n: int, *, critical_ids: set[str] | None = None) -> list[ReplayCase]:
    return [
        ReplayCase(case_id=f"case_{i:03d}", critical=(f"case_{i:03d}" in (critical_ids or set())))
        for i in range(n)
    ]


def test_candidate_improvement_promotes_with_sufficient_evidence() -> None:
    promoter = ReplayPromoter(min_cases=3, min_win_rate=0.6)
    cases = [ReplayCase(case_id=f"case_{i:03d}") for i in range(4)]
    incumbent = [
        ReplayCaseResult(model_id="inc", case_id=f"case_{i:03d}", success=False)
        for i in range(4)
    ]
    candidate = [
        ReplayCaseResult(model_id="cand", case_id=f"case_{i:03d}", success=True)
        for i in range(4)
    ]
    result = promoter.evaluate("inc", "cand", incumbent, candidate, cases)
    assert result.decision is ReplayPromotionDecision.PROMOTE
    assert result.comparison.cases_evaluated == 4  # same cases, both models
    assert result.comparison.candidate_win_rate == 1.0
    assert len(result.reasons) >= 2  # auditable reason trail


def test_candidate_regression_on_critical_case_blocks_promotion() -> None:
    promoter = ReplayPromoter(min_cases=3, min_win_rate=0.6)
    cases = [ReplayCase(case_id="case_000", critical=True)] + [
        ReplayCase(case_id=f"case_{i:03d}") for i in range(1, 4)
    ]
    incumbent = [
        ReplayCaseResult(model_id="inc", case_id="case_000", success=True),
        *[
            ReplayCaseResult(model_id="inc", case_id=f"case_{i:03d}", success=False)
            for i in range(1, 4)
        ],
    ]
    candidate = [
        ReplayCaseResult(model_id="cand", case_id=f"case_{i:03d}", success=(i != 0))
        for i in range(4)
    ]
    result = promoter.evaluate("inc", "cand", incumbent, candidate, cases)
    assert result.decision is ReplayPromotionDecision.REJECT
    assert "critical regression" in result.reasons[0]


def test_any_regression_blocks_promotion() -> None:
    promoter = ReplayPromoter(min_cases=3, min_win_rate=0.6)
    # incumbent wins exactly one case; the candidate wins the other three
    # but regresses on the incumbent's win → promotion blocked
    incumbent = [
        ReplayCaseResult(model_id="inc", case_id="case_000", success=True),
        *[
            ReplayCaseResult(model_id="inc", case_id=f"case_{i:03d}", success=False)
            for i in range(1, 5)
        ],
    ]
    candidate = [
        ReplayCaseResult(model_id="cand", case_id="case_000", success=False),
        *[
            ReplayCaseResult(model_id="cand", case_id=f"case_{i:03d}", success=True)
            for i in range(1, 5)
        ],
    ]
    result = promoter.evaluate("inc", "cand", incumbent, candidate)
    assert result.decision is ReplayPromotionDecision.REJECT
    assert "regressions" in result.reasons[0]
    assert result.comparison.incumbent_wins == 1


def test_one_successful_case_never_promotes() -> None:
    promoter = ReplayPromoter(min_cases=3)
    incumbent = [ReplayCaseResult(model_id="inc", case_id="case_000", success=False)]
    candidate = [ReplayCaseResult(model_id="cand", case_id="case_000", success=True)]
    result = promoter.evaluate("inc", "cand", incumbent, candidate)
    assert result.decision is ReplayPromotionDecision.INCONCLUSIVE
    assert "one case never promotes" in result.reasons[0]


def test_insufficient_evidence_is_inconclusive_not_promoted() -> None:
    promoter = ReplayPromoter(min_cases=5)
    incumbent = [ReplayCaseResult(model_id="inc", case_id=f"c{i}", success=False) for i in range(2)]
    candidate = [ReplayCaseResult(model_id="cand", case_id=f"c{i}", success=True) for i in range(2)]
    result = promoter.evaluate("inc", "cand", incumbent, candidate)
    assert result.decision is ReplayPromotionDecision.INCONCLUSIVE


def test_replay_is_deterministic_and_auditable() -> None:
    promoter = ReplayPromoter(min_cases=3)
    cases = [ReplayCase(case_id=f"c{i}") for i in range(4)]
    inc = [ReplayCaseResult(model_id="inc", case_id=f"c{i}", success=False) for i in range(4)]
    cand = [ReplayCaseResult(model_id="cand", case_id=f"c{i}", success=True) for i in range(4)]
    first = promoter.evaluate("inc", "cand", inc, cand, cases)
    second = promoter.evaluate("inc", "cand", inc, cand, cases)
    assert first == second  # deterministic
    assert first.comparison.cases_evaluated == 4  # audit counts
    assert first.comparison.regressions == []  # auditable evidence trail


def test_candidate_improvement_vs_regression_scenarios() -> None:
    promoter = ReplayPromoter(min_cases=3, min_win_rate=0.6)
    cases = [ReplayCase(case_id=f"c{i}", critical=False) for i in range(4)]
    # improvement: candidate wins all like-for-like cases
    inc_losing = [
        ReplayCaseResult(model_id="inc", case_id=f"c{i}", success=False) for i in range(4)
    ]
    cand_winning = [
        ReplayCaseResult(model_id="cand", case_id=f"c{i}", success=True) for i in range(4)
    ]
    promote = promoter.evaluate("inc", "cand", inc_losing, cand_winning, cases)
    assert promote.decision is ReplayPromotionDecision.PROMOTE
    # candidate regression everywhere: rejected
    inc_winning = [
        ReplayCaseResult(model_id="inc", case_id=f"c{i}", success=True) for i in range(4)
    ]
    cand_failing = [
        ReplayCaseResult(model_id="cand", case_id=f"c{i}", success=False) for i in range(4)
    ]
    result = promoter.evaluate("inc", "cand", inc_winning, cand_failing, cases)
    assert result.decision is ReplayPromotionDecision.REJECT
    # insufficient evidence: only 2 shared cases
    short = [
        ReplayCaseResult(model_id="cand", case_id="c0", success=True),
        ReplayCaseResult(model_id="cand", case_id="c1", success=True),
    ]
    short_run = promoter.evaluate("inc", "cand", inc_losing, short, cases)
    assert short_run.decision is ReplayPromotionDecision.INCONCLUSIVE


def test_foreign_evidence_never_blends() -> None:
    promoter = ReplayPromoter(min_cases=3, min_win_rate=0.6)
    # incumbent results attributed to another model are IGNORED, not blended
    incumbent = [
        ReplayCaseResult(model_id="other", case_id=f"c{i}", success=True) for i in range(4)
    ]
    candidate = [
        ReplayCaseResult(model_id="cand", case_id=f"c{i}", success=True) for i in range(4)
    ]
    result = promoter.evaluate("inc", "cand", incumbent, candidate)
    assert result.decision is ReplayPromotionDecision.INCONCLUSIVE  # no like-for-like cases


def test_same_model_rejected_and_fail_closed() -> None:
    promoter = ReplayPromoter()
    result = promoter.evaluate("m", "m", [], [])
    assert result.decision is ReplayPromotionDecision.REJECT
    assert "failed closed" in result.reasons[0]
