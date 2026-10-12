"""Cognitive capability benchmarks (Sprint 7): capabilities are MEASURED,
never declared by model name (SPEC §12, §52A).

A suite is a bounded, deterministic, ordered set of probe cases per
capability (structured output, planning, tool reasoning, ...). Execution
goes through the EXISTING ``BenchmarkRunner``/``InferenceEngine``
boundary (mock engine in tests — no live provider requirement) and
records raw ``BenchmarkRecord`` evidence through the existing
``ModelRegistry.record_benchmark`` path, so routing consumes the same
engine-scoped, version-scoped bundles as every other measurement.

Bounded: at most ``MAX_SUITE_CASES`` cases per run; a suite larger than
the bound is truncated deterministically (stable order, never shuffled).
Deterministic: case order and category mapping are fixed; failure of one
case records an honest failure and continues — a benchmark never breaks
a task and never invents a score.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import CapabilityTag, ModelRecord
from nomadicos.evaluation.benchmarking import BenchmarkRunner, ProbeCase

MAX_SUITE_CASES = 32

#: capability → probe category mapping into the existing benchmark
#: vocabulary (deterministic, fixed)
_CATEGORY: dict[str, str] = {
    CapabilityTag.TEXT.value: "bug_fix",
    CapabilityTag.REASONING.value: "debugging",
    CapabilityTag.TOOL_USE.value: "bug_fix",
    CapabilityTag.CODING.value: "feature_implementation",
    CapabilityTag.TESTING.value: "test_generation",
}


class CapabilitySuiteCase(ProbeCase):
    """One probe case with its target capability (Sprint 7).

    Extends the existing ``ProbeCase`` (prompt + must/must-not contain)
    with the measured capability dimension."""

    capability: str = Field(default=CapabilityTag.TEXT.value)

    @model_validator(mode="after")
    def _known_capability(self) -> CapabilitySuiteCase:
        known = {t.value for t in CapabilityTag}
        if self.capability not in known:
            raise ValueError(f"unknown capability {self.capability!r}")
        return self


class CapabilityBenchmarkSuite:
    """Bounded deterministic capability measurement for one model."""

    def __init__(self, benchmark_version: str, *, max_cases: int = MAX_SUITE_CASES) -> None:
        if max_cases < 1:
            raise ValueError("max_cases must be >= 1")
        self._version = benchmark_version
        self._max = max_cases

    async def run(
        self,
        runner: BenchmarkRunner,
        model: ModelRecord,
        cases: list[CapabilitySuiteCase],
    ) -> list[BenchmarkRecord]:
        """Run the suite through the EXISTING benchmark boundary.

        Deterministic order (capability, then case id; truncated to the
        bound); each case is one honest measured probe recorded through
        the runner's registry path. The runner is external: this suite
        never instantiates engines and never fabricates a score."""
        if not cases:
            return []
        ordered = sorted(cases, key=lambda c: (c.capability, c.id))[: self._max]
        records: list[BenchmarkRecord] = []
        for case in ordered:
            category = _CATEGORY.get(case.capability)
            if category is None:
                # unknown capability: fail closed, never silently misfile
                raise ValueError(f"unmapped capability {case.capability!r}")
            probe = ProbeCase(
                id=f"{case.capability}:{case.id}",
                prompt=case.prompt,
                must_contain=list(case.must_contain),
                must_not_contain=list(case.must_not_contain),
                category=category,
            )
            records.append(await runner.probe(model, probe))
        return records


__all__ = [
    "MAX_SUITE_CASES",
    "CapabilityBenchmarkSuite",
    "CapabilitySuiteCase",
]
