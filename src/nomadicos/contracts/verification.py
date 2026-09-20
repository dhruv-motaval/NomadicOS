"""Verification contracts (SPEC §27-29, §56.8-10) and the critic contract (§16)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id


class VerificationLevel(StrEnum):
    STEP = "STEP"
    GOAL = "GOAL"


class VerificationOutcome(StrEnum):
    """Phase 8 (§8.2/§8.10): four DISTINCT non-collapsible verdicts."""

    PASS = "PASS"
    NOT_PASS = "NOT_PASS"
    NOT_VERIFIED = "NOT_VERIFIED"
    BLOCKED = "BLOCKED"


class EvidenceItem(Contract):
    """One observed fact. `observed=True` requires real-world evidence.

    `unverifiable=True` marks an evaluation that COULD NOT be performed
    (missing predicate, absent evidence source) — never a silent pass.
    """

    claim: str
    observed: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    unverifiable: bool = False


class VerificationResult(Contract):
    """Result of a step or goal verification pass.

    A passed result is impossible without evidence — enforced at construct
    time (SPEC §56.10, §14). The verifier identity/version is attributable
    (SPEC §8.23); results are immutable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("verif"))
    level: VerificationLevel
    task_id: str
    step_id: str | None = None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: outcome may be explicit (real verifiers) or derived (legacy/placeholder)
    outcome: VerificationOutcome | None = None
    verifier: str = "legacy-placeholder"

    @model_validator(mode="after")
    def _coherent(self) -> VerificationResult:
        if not self.evidence:
            raise ValueError("a verification with no evidence proves nothing (SPEC §28)")
        all_observed = all(item.observed for item in self.evidence)
        if self.outcome is VerificationOutcome.PASS and not all_observed:
            raise ValueError("PASS requires every evidence item observed (SPEC §8.11)")
        if (
            self.outcome in (VerificationOutcome.NOT_PASS, VerificationOutcome.BLOCKED)
            and all_observed
        ):
            raise ValueError(f"{self.outcome.value} contradicts fully-observed evidence")
        if self.outcome is VerificationOutcome.NOT_VERIFIED and all_observed:
            raise ValueError("NOT_VERIFIED contradicts fully-observed evidence")
        return self

    @property
    def passed(self) -> bool:
        return self.verdict is VerificationOutcome.PASS

    @property
    def verdict(self) -> VerificationOutcome:
        if self.outcome is not None:
            return self.outcome
        if all(item.observed for item in self.evidence):
            return VerificationOutcome.PASS
        if any(item.unverifiable for item in self.evidence):
            return VerificationOutcome.NOT_VERIFIED
        return VerificationOutcome.NOT_PASS

    @property
    def missing(self) -> list[str]:
        return [item.claim for item in self.evidence if not item.observed]

    def why(self, limit: int = 4) -> list[str]:
        """Concise human-readable explanation (SPEC §8.32). No giant dumps."""
        lines: list[str] = []
        for item in self.evidence[:limit]:
            mark = "PASS" if item.observed else ("UNVERIFIED" if item.unverifiable else "FAIL")
            detail = " ".join(f"{k}={v}" for k, v in list(item.detail.items())[:4])
            lines.append(f"{mark}: {item.claim[:100]} {detail[:160]}".rstrip())
        if len(self.evidence) > limit:
            lines.append(f"... {len(self.evidence) - limit} more evidence items")
        return lines


class CriticDecision(StrEnum):
    ACCEPT = "ACCEPT"
    IMPROVE = "IMPROVE"
    REJECT = "REJECT"  # BLOCK semantics: critical defect prevents acceptance
    #: critic model unavailable / output unparseable: NOT a positive result,
    #: never an ACCEPT (SPEC §10.11, §10.26)
    NOT_EVALUATED = "NOT_EVALUATED"


class CriticReport(Contract):
    """Structured critic output (SPEC §16, §10.2). A bare score is not accepted.

    ``tests_passed``/``goal_verified`` are SYSTEM-recorded evidence flags
    injected by the evaluation harness from real executions/verifications -
    the critic model cannot set them (they are forbidden in model output),
    and ``ACCEPT`` is constructible only with both True (SPEC §16 acceptance
    rule: score + tests + goal + no critical defects).
    """

    id: str = Field(default_factory=lambda: new_id("critic"))
    model_id: str
    task_id: str
    iteration: int = 1
    decision: CriticDecision
    score: float | None = Field(default=None, ge=0.0, le=10.0)
    critical_issues: list[str] = Field(default_factory=list)
    major_issues: list[str] = Field(default_factory=list)
    minor_issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    #: references to actual evidence (execution/verification ids, paths)
    evidence_refs: list[str] = Field(default_factory=list)
    #: Engineering evidence the critic actually observed: tests run/passed etc.
    tests_passed: bool | None = None
    goal_verified: bool | None = None
    #: true when the model claimed ACCEPT but system evidence suppressed it
    accept_suppressed: bool = False

    @model_validator(mode="after")
    def _accept_requires_evidence(self) -> CriticReport:
        if self.decision is CriticDecision.ACCEPT:
            if self.critical_issues:
                raise ValueError("ACCEPT with critical issues violates SPEC §16")
            if self.tests_passed is not True or self.goal_verified is not True:
                raise ValueError("acceptance requires tests passing AND goal verified (SPEC §16)")
        if self.decision is CriticDecision.NOT_EVALUATED and self.score is not None:
            raise ValueError("NOT_EVALUATED must not carry a score (SPEC §10.11)")
        return self

    def summary(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision.value,
            "critical": self.critical_issues,
            "major": self.major_issues,
            "minor": self.minor_issues,
            "required_tests": self.required_tests,
        }


class ClaimState(StrEnum):
    """Honesty vocabulary for our own completion reporting (SPEC §51)."""

    IMPLEMENTED = "IMPLEMENTED"
    TESTED = "TESTED"
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


Verdict = Literal["PASS", "NOT_PASS"]
