"""Sprint 7 — cognitive capability benchmarks: deterministic, bounded,
honest-failure measurement through the EXISTING benchmark boundary."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from nomadicos.contracts.model import ModelHealth, ModelRecord
from nomadicos.evaluation.benchmarking import BenchmarkRunner
from nomadicos.evaluation.cognitive import (
    MAX_SUITE_CASES,
    CapabilityBenchmarkSuite,
    CapabilitySuiteCase,
)
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.events import EventLogger
from nomadicos.registry.model_registry import ModelRegistry


def model() -> ModelRecord:
    return ModelRecord(
        model_id="suite-m",
        engine="mock",
        roles=["worker"],
        capabilities=["text", "reasoning"],
        health=ModelHealth.HEALTHY,
    )


def runner(
    registry: ModelRegistry | None = None, engine: MockEngine | None = None
) -> BenchmarkRunner:
    return BenchmarkRunner(
        registry or ModelRegistry(EventLogger()), engine or MockEngine(), "cog-v1"
    )


def cases() -> list[CapabilitySuiteCase]:
    return [
        CapabilitySuiteCase(
            id="p1", capability="reasoning", prompt="plan X", must_contain=["step"]
        ),
        CapabilitySuiteCase(id="p2", capability="text", prompt="write Y", must_contain=["done"]),
    ]


def test_deterministic_benchmark_results() -> None:
    suite = CapabilityBenchmarkSuite("cog-v1")

    def run() -> list:
        engine = MockEngine(default_response="step done")
        return asyncio.run(suite.run(runner(engine=engine), model(), cases()))

    first = run()
    second = run()
    assert [r.success for r in first] == [True, True]

    def outcome(rs: list) -> list:
        return [(r.task_id, r.task_category, r.success, r.failure_type) for r in rs]

    # the MEASURED OUTCOME is deterministic (ids/timestamps are honest
    # per-record metadata, not repeated measurements)
    assert outcome(first) == outcome(second)


def test_capability_scoring_reflects_outcomes() -> None:
    suite = CapabilityBenchmarkSuite("cog-v1")
    mixed = [
        CapabilitySuiteCase(id="hit", capability="reasoning", prompt="probe", must_contain=["ok"]),
        CapabilitySuiteCase(
            id="miss", capability="reasoning", prompt="probe", must_contain=["impossible-needle"]
        ),
    ]
    records = asyncio.run(
        suite.run(runner(engine=MockEngine(default_response="ok")), model(), mixed)
    )
    successes = [r.success for r in records]
    assert True in successes and False in successes  # honest mixed outcomes
    failed = next(r for r in records if not r.success)
    assert failed.failure_type == "WRONG_OUTPUT"  # honest failure recording


def test_bounded_execution_truncates_deterministically() -> None:
    suite = CapabilityBenchmarkSuite("cog-v1", max_cases=4)
    many = [
        CapabilitySuiteCase(id=f"case_{i}", capability="text", prompt="p")
        for i in range(MAX_SUITE_CASES + 10)
    ]
    records = asyncio.run(suite.run(runner(), model(), many))
    assert len(records) == 4  # hard bound
    assert [r.task_id for r in records] == sorted(r.task_id for r in records)


def test_benchmark_failure_recorded_honestly() -> None:
    suite = CapabilityBenchmarkSuite("cog-v1")
    engine = MockEngine()
    engine.fail_models.add("suite-m")
    records = asyncio.run(suite.run(runner(engine=engine), model(), cases()))
    assert len(records) == 2
    assert all(r.success is False for r in records)
    assert all(r.failure_type == "MODEL_ERROR" for r in records)  # honest, never skipped


def test_missing_benchmark_means_no_evidence() -> None:
    registry = ModelRegistry(EventLogger())
    bundle = registry.bundle_for("ghost-m", "mock", "bug_fix")
    assert bundle.samples == 0
    assert bundle.success_rate is None  # nothing invented (SPEC §12)


def test_registry_evidence_flows_to_routing() -> None:
    """Suite records → registry → routing bundles (same evidence path).
    The runner records into the wired registry itself — no double-write."""
    registry = ModelRegistry(EventLogger())
    suite = CapabilityBenchmarkSuite("cog-v1")
    records = asyncio.run(
        suite.run(
            runner(registry=registry, engine=MockEngine(default_response="fine ok")),
            model(),
            [CapabilitySuiteCase(id="ok", capability="text", prompt="p", must_contain=["fine"])],
        )
    )
    assert len(records) == 1
    bundle = registry.bundle_for("suite-m", "mock", "bug_fix")
    assert bundle.samples == 1
    assert bundle.success_rate == 1.0  # measured, not declared


def test_capability_case_validation() -> None:
    # Sprint 7 extension: unknown capabilities are rejected (fail closed)
    with pytest.raises(ValidationError):
        CapabilitySuiteCase(id="x", capability="telepathy", prompt="p")
    # unknown capability via model_validate too (closed vocabulary)
    with pytest.raises(ValidationError):
        CapabilitySuiteCase.model_validate({"id": "x", "capability": "faith", "prompt": "p"})
