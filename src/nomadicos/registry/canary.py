"""Model canary (Sprint 7): a candidate NEVER becomes the default merely
because a benchmark score is higher. Promotion requires a bounded
evaluation window of real outcomes; the incumbent is preserved until —
and unless — deterministic promotion criteria are met; candidate failure
rolls back to the incumbent. Fail closed: any evaluation problem keeps
the incumbent. Decisions are deterministic and auditable; no external
model calls are required (evidence arrives as recorded outcomes).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract

#: bounded canary window defaults (hard-capped; a canary never runs forever)
DEFAULT_MIN_SAMPLES = 3
DEFAULT_MAX_SAMPLES = 100
#: candidate must strictly beat the incumbent by at least this margin to
#: promote (ties and noise keep the incumbent — conservative by design)
DEFAULT_PROMOTION_MARGIN = 0.1
#: absolute quality bar when NO incumbent evidence exists — an unknown
#: incumbent never lowers the bar to near-zero
DEFAULT_ABSOLUTE_BAR = 0.6


class CanaryDecision(StrEnum):
    PROMOTE = "PROMOTE"
    KEEP_INCUMBENT = "KEEP_INCUMBENT"
    ROLLBACK = "ROLLBACK"
    INCONCLUSIVE = "INCONCLUSIVE"


class CanarySample(Contract):
    """One canary observation: a real outcome for the CANDIDATE model on
    one case. DATA only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    success: bool
    score: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> CanarySample:
        if not self.case_id.strip():
            raise ValueError("canary sample case_id must not be empty")
        return self


class CanaryEvaluation(Contract):
    """Deterministic canary verdict with its full evidence trail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    incumbent_model_id: str
    candidate_model_id: str
    window_size: int = Field(ge=0)
    candidate_successes: int = Field(ge=0)
    candidate_failures: int = Field(ge=0)
    incumbent_reference_rate: float | None = None
    candidate_success_rate: float | None = None
    decision: CanaryDecision
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> CanaryEvaluation:
        if len(self.reasons) > 8:
            raise ValueError("canary reasons exceed 8")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("canary reasons must not be empty")
        return self


class CanaryEvaluator:
    """Bounded, deterministic canary decisions. Fail closed: every error
    path returns KEEP_INCUMBENT — a broken canary can never promote."""

    def __init__(
        self,
        *,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        promotion_margin: float = DEFAULT_PROMOTION_MARGIN,
        incumbent_reference_rate: float | None = None,
    ) -> None:
        if min_samples < 1 or max_samples < min_samples:
            raise ValueError("canary window bounds are incoherent")
        if not 0.0 <= promotion_margin <= 1.0:
            raise ValueError("promotion margin must be within [0, 1]")
        self._min = min_samples
        self._max = max_samples
        self._margin = promotion_margin
        self._incumbent_rate = incumbent_reference_rate

    def evaluate(
        self,
        incumbent_model_id: str,
        candidate_model_id: str,
        samples: list[CanarySample],
    ) -> CanaryEvaluation:
        """Deterministic verdict over the bounded window.

        - fewer than ``min_samples`` → INCONCLUSIVE (incumbent preserved;
          no automatic promotion without sufficient evidence)
        - window capped at ``max_samples`` (bounded)
        - candidate success rate must beat the incumbent reference by
          ``promotion_margin`` → PROMOTE; otherwise KEEP_INCUMBENT
        - any failure → KEEP_INCUMBENT (fail closed)
        """
        try:
            return self._evaluate(incumbent_model_id, candidate_model_id, samples)
        except Exception as exc:  # fail closed: keep the incumbent
            return CanaryEvaluation(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                window_size=0,
                candidate_successes=0,
                candidate_failures=0,
                incumbent_reference_rate=self._incumbent_rate,
                decision=CanaryDecision.KEEP_INCUMBENT,
                reasons=[f"canary failed closed: {type(exc).__name__}"],
            )

    def _evaluate(
        self,
        incumbent_model_id: str,
        candidate_model_id: str,
        samples: list[CanarySample],
    ) -> CanaryEvaluation:
        if incumbent_model_id == candidate_model_id:
            raise ValueError("canary requires distinct incumbent and candidate")
        # deterministic window: case_id order, hard-capped
        window = sorted(samples, key=lambda s: s.case_id)[: self._max]
        size = len(window)
        successes = sum(1 for s in window if s.success)
        failures = size - successes
        if size < self._min:
            return CanaryEvaluation(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                window_size=size,
                candidate_successes=successes,
                candidate_failures=failures,
                incumbent_reference_rate=self._incumbent_rate,
                decision=CanaryDecision.INCONCLUSIVE,
                reasons=[
                    (
                        f"insufficient evidence: {size} < {self._min} samples; "
                        f"incumbent preserved (no promotion without evidence)"
                    )
                ],
            )
        rate = round(successes / size, 4)
        reference = (
            self._incumbent_rate
            if self._incumbent_rate is not None
            else DEFAULT_ABSOLUTE_BAR  # unknown incumbent: absolute quality bar
        )
        if rate >= reference + self._margin:
            return CanaryEvaluation(
                incumbent_model_id=incumbent_model_id,
                candidate_model_id=candidate_model_id,
                window_size=size,
                candidate_successes=successes,
                candidate_failures=failures,
                incumbent_reference_rate=self._incumbent_rate,
                candidate_success_rate=rate,
                decision=CanaryDecision.PROMOTE,
                reasons=[
                    (
                        f"candidate {rate:.4f} >= incumbent {reference:.4f} "
                        f"+ margin {self._margin:.2f}"
                    ),
                    f"window {size} samples (bounded)",
                ],
            )
        return CanaryEvaluation(
            incumbent_model_id=incumbent_model_id,
            candidate_model_id=candidate_model_id,
            window_size=size,
            candidate_successes=successes,
            candidate_failures=failures,
            incumbent_reference_rate=self._incumbent_rate,
            candidate_success_rate=rate,
            decision=CanaryDecision.KEEP_INCUMBENT,
            reasons=[
                f"candidate {rate:.4f} below incumbent {reference:.4f} + margin "
                f"{self._margin:.2f}; incumbent preserved",
            ],
        )


__all__ = [
    "DEFAULT_MAX_SAMPLES",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_PROMOTION_MARGIN",
    "CanaryDecision",
    "CanaryEvaluation",
    "CanaryEvaluator",
    "CanarySample",
]
