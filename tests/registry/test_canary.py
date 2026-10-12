"""Sprint 7 — model canary: bounded window, deterministic decisions,
fail-closed, incumbent preserved on failure."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.registry.canary import (
    CanaryDecision,
    CanaryEvaluator,
    CanarySample,
)


def samples(n: int, *, successes: int | None = None) -> list[CanarySample]:
    return [
        CanarySample(case_id=f"case_{i:03d}", success=(successes is None or i < successes))
        for i in range(n)
    ]


def test_incumbent_preserved_initially_insufficient_evidence() -> None:
    evaluator = CanaryEvaluator(min_samples=5)
    verdict = evaluator.evaluate("incumbent", "candidate", samples(2, successes=2))
    assert verdict.decision is CanaryDecision.INCONCLUSIVE
    assert verdict.incumbent_model_id == "incumbent"  # preserved
    assert "insufficient evidence" in verdict.reasons[0]


def test_candidate_promotion_requires_full_margin() -> None:
    evaluator = CanaryEvaluator(min_samples=3, promotion_margin=0.1)
    verdict = evaluator.evaluate("inc", "cand", samples(10, successes=10))
    assert verdict.decision is CanaryDecision.PROMOTE
    assert verdict.candidate_success_rate == 1.0
    assert verdict.window_size == 10  # bounded window recorded


def test_candidate_below_incumbent_keeps_incumbent() -> None:
    evaluator = CanaryEvaluator(min_samples=3, promotion_margin=0.1,
                                incumbent_reference_rate=0.9)
    verdict = evaluator.evaluate("inc", "cand", samples(10, successes=8))
    assert verdict.decision is CanaryDecision.KEEP_INCUMBENT
    assert verdict.candidate_success_rate == 0.8


def test_window_is_hard_capped() -> None:
    evaluator = CanaryEvaluator(min_samples=3, max_samples=5)
    verdict = evaluator.evaluate("inc", "cand", samples(50, successes=50))
    assert verdict.window_size == 5  # bounded, never runs forever


def test_candidate_failure_preserves_incumbent() -> None:
    evaluator = CanaryEvaluator(min_samples=3)
    verdict = evaluator.evaluate("inc", "cand", samples(6, successes=1))
    assert verdict.decision is CanaryDecision.KEEP_INCUMBENT


def test_canary_is_deterministic() -> None:
    evaluator = CanaryEvaluator(min_samples=3)
    first = evaluator.evaluate("inc", "cand", samples(8, successes=7))
    second = evaluator.evaluate("inc", "cand", samples(8, successes=7))
    assert first == second


def test_canary_fails_closed_on_error() -> None:
    class Exploding:
        @property
        def case_id(self) -> str:
            raise RuntimeError("boom")

        success = True

    evaluator = CanaryEvaluator(min_samples=3)
    verdict = evaluator.evaluate("inc", "cand", [Exploding()])  # type: ignore[list-item]
    assert verdict.decision is CanaryDecision.KEEP_INCUMBENT  # never promotes on error
    assert "failed closed" in verdict.reasons[0]


def test_canary_rejects_incoherent_configuration() -> None:
    with pytest.raises(ValueError):
        CanaryEvaluator(min_samples=0)
    with pytest.raises(ValueError):
        CanaryEvaluator(min_samples=10, max_samples=5)
    with pytest.raises(ValueError):
        CanaryEvaluator(promotion_margin=1.5)


def test_canary_contracts_are_closed() -> None:
    with pytest.raises(ValidationError):
        CanarySample(case_id=" ", success=True)
    with pytest.raises(ValidationError):
        CanarySample(case_id="c", success=True, score=2.0)
    # authority-shaped extras rejected
    with pytest.raises(ValidationError):
        CanarySample.model_validate({"case_id": "c", "success": True, "authorized": True})


def test_rollback_decision_available_for_promoted_candidate() -> None:
    # a promoted candidate that later degrades rolls back explicitly
    assert CanaryDecision.ROLLBACK.value == "ROLLBACK"
