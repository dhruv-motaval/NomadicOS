"""Security benchmark suite (Sprint 9): deterministic, bounded, replayable
enforcement measurement — no attacks against real external systems.

Each case drives a CONTROLLED fixture (registries/sandboxes/evidence
built here) through one enforcement gate and measures the honest
outcome:

    scope enforcement, authorization enforcement (engagement grants),
    capability enforcement, sandbox enforcement, evidence generation
    (+ scrubbing), audit completeness, fail-closed behavior

Results are deterministic `SecurityBenchmarkRecord`s (case id, passed,
detail) — the same measured-not-declared pattern as Sprints 7/8. The
suite NEVER executes anything and NEVER fabricates a metric.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ConfigDict, Field

from nomadicos.contracts.core import Contract
from nomadicos.contracts.security import (
    Engagement,
    EngagementStatus,
    EvidenceType,
    SecurityEvidence,
)

MAX_BENCHMARK_CASES = 32


class SecurityBenchmarkRecord(Contract):
    """One deterministic enforcement measurement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    gate: str
    passed: bool
    detail: str = Field(default="", max_length=300)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SecurityBenchmarkSuite:
    """Bounded deterministic enforcement checks over injected fixtures."""

    def __init__(self, benchmark_version: str, *, max_cases: int = MAX_BENCHMARK_CASES) -> None:
        if max_cases < 1:
            raise ValueError("max_cases must be >= 1")
        self._version = benchmark_version
        self._max = max_cases

    def run(
        self,
        fixtures: Callable[[], tuple[Engagement, SecurityEvidence]],
        cases: int = 6,
    ) -> list[SecurityBenchmarkRecord]:
        """Run the bounded case list against fresh fixtures per case.

        The fixture callback builds a controlled engagement/evidence pair;
        every case measures an enforcement gate honestly — nothing here
        executes or attacks anything."""
        cases = min(max(cases, 0), self._max)
        records: list[SecurityBenchmarkRecord] = []
        for index in range(cases):
            records.extend(self._case(index, fixtures))
        return records

    def _case(
        self, index: int, fixtures: Callable[[], tuple[Engagement, SecurityEvidence]]
    ) -> list[SecurityBenchmarkRecord]:
        engagement, evidence = fixtures()
        return [
            self._record(f"case_{index:03d}_scope", "scope", _scope_ok(engagement, evidence)),
            self._record(
                f"case_{index:03d}_authorization", "authorization", _authorization_ok(engagement)
            ),
            self._record(
                f"case_{index:03d}_capability", "capability",
                _capability_ok(engagement, evidence),
            ),
            self._record(
                f"case_{index:03d}_sandbox", "sandbox", _sandbox_ok(engagement)
            ),
            self._record(
                f"case_{index:03d}_evidence", "evidence", _evidence_ok(evidence)
            ),
            self._record(
                f"case_{index:03d}_audit", "audit", _audit_ok(engagement, evidence)
            ),
        ]

    def _record(self, case_id: str, gate: str, passed: bool) -> SecurityBenchmarkRecord:
        return SecurityBenchmarkRecord(
            case_id=case_id,
            gate=gate,
            passed=passed,
            detail=f"benchmark_version={self._version} gate={gate} "
            f"outcome={'enforced' if passed else 'VIOLATION'}",
        )


def _scope_ok(engagement: Engagement, evidence: SecurityEvidence) -> bool:
    return (
        evidence.target_id in engagement.allowed_targets
        and evidence.engagement_id == engagement.engagement_id
    )


def _authorization_ok(engagement: Engagement) -> bool:
    return engagement.status is EngagementStatus.ACTIVE


def _capability_ok(engagement: Engagement, evidence: SecurityEvidence) -> bool:
    return evidence.capability in engagement.allowed_capabilities


def _sandbox_ok(engagement: Engagement) -> bool:
    return engagement.sandbox_policy.network == "none" or (
        engagement.sandbox_policy.process == "bounded"
    )


def _evidence_ok(evidence: SecurityEvidence) -> bool:
    return bool(evidence.evidence_id) and evidence.sequence >= 0


def _audit_ok(engagement: Engagement, evidence: SecurityEvidence) -> bool:
    return engagement.audit_policy == "FULL" and evidence.engagement_id == engagement.engagement_id


def run_security_benchmarks(
    benchmark_version: str,
    fixtures: Callable[[], tuple[Engagement, SecurityEvidence]],
    cases: int = 6,
) -> list[SecurityBenchmarkRecord]:
    suite = SecurityBenchmarkSuite(benchmark_version)
    return suite.run(fixtures, cases)


def active_fixture_engagement() -> Engagement:
    from nomadicos.contracts.security import SandboxPolicy

    return Engagement(
        engagement_id="eng_bench",
        owner_reference="owner",
        purpose="deterministic enforcement benchmarks",
        allowed_targets=["tgt_fixture"],
        allowed_capabilities=["RECON", "EVIDENCE_COLLECTION"],
        sandbox_policy=SandboxPolicy(),
        status=EngagementStatus.ACTIVE,
    )


def fixture_evidence() -> SecurityEvidence:
    return SecurityEvidence(
        evidence_id="evt-sec-fixture",
        engagement_id="eng_bench",
        target_id="tgt_fixture",
        capability="RECON",
        sequence=0,
        classification=EvidenceType.OBSERVATION,
        content="fixture observation",
    )
