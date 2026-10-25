"""Focused regression coverage (Phase 14C-C): metric freshness policy.

The Phase 14C routing audit CONFIRMED that stored timestamps existed but no
freshness policy existed: old evidence weighed the same as new evidence.
14C-C adds a configurable registry TTL policy: records older than
``now - metric_ttl_s`` are excluded from every aggregation — expired
evidence can never dominate fresh evidence; a record with unknown age
(None timestamp) is never expired; all evidence stale means an empty bundle
(deterministic cold start). Tests use a FIXED injected clock - no sleeps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.router import ModelSelector

FIXED_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
TTL_S = 3600.0


def clock_at(now: datetime = FIXED_NOW):
    return lambda: now


def reg_with_ttl(ttl: float | None = TTL_S, now: datetime = FIXED_NOW) -> ModelRegistry:
    return ModelRegistry(EventLogger(), metric_ttl_s=ttl, clock=clock_at(now))


def rec(
    mid: str,
    age_s: float | None,
    success: bool = True,
    engine: str = "ollama",
    version: str = "bench-v1",
    category: str = "bug_fix",
) -> BenchmarkRecord:
    return BenchmarkRecord(
        id=f"bench_{engine}_{int(success)}_{category}_{age_s}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version=version,
        task_id="t",
        task_category=category,  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=success,
        total_latency_ms=100.0,
        timestamp=None if age_s is None else FIXED_NOW - timedelta(seconds=age_s),
    )


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def register_worker(reg: ModelRegistry, mid: str = "X", engine: str = "ollama") -> None:
    reg.register(
        ModelRecord(
            model_id=mid,
            engine=engine,  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
        )
    )


# ------------------------------------------------------------ freshness ----


def test_fresh_evidence_remains_usable() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=10.0))  # 10s old, TTL 1h: fresh
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    assert bundle.samples == 1
    assert bundle.success_rate == 1.0


def test_age_under_ttl_is_kept() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S - 1.0))  # 1s inside the TTL
    assert reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark").samples == 1


def test_age_equal_to_ttl_is_kept() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S))  # exactly at the boundary
    assert reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark").samples == 1


def test_age_beyond_ttl_is_expired() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S + 1.0))  # 1s past the TTL
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    assert bundle.samples == 0
    assert bundle.success_rate is None  # never invented


def test_expired_evidence_cannot_dominate_fresh() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S * 100.0, success=False))  # ancient failure
    reg.record_benchmark(rec("X", age_s=5.0, success=True))  # fresh success
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    # ONLY the fresh sample counts: the expired failure cannot drag the rate
    assert bundle.samples == 1
    assert bundle.success_rate == 1.0


def test_all_stale_is_explicit_cold_start() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S + 1.0, success=True))
    reg.record_benchmark(rec("X", age_s=TTL_S * 10.0, success=True))
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    assert bundle.samples == 0  # explicit: no usable evidence at all
    # deterministic cold start: the candidate stays ranked, unmeasured
    ranked = ModelSelector(RouterConfig()).rank(reg, reqs())
    scored = [c for c in ranked if c.model_id == "X"]
    assert scored and scored[0].measured is False


def test_unknown_age_is_never_expired() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    # legacy production record: unknown age (recorded_at None) — never
    # treated as stale and never invented
    reg.record_production(
        ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True)
    )
    assert reg.bundle_for("X", "ollama", "coding").samples == 1
    # benchmark records always carry a stamped timestamp (never None)
    stamped = reg.metrics("X", "ollama", benchmark_version="bench-v1").benchmark
    assert stamped == {}  # the production record is not benchmark evidence


def test_no_evidence_remains_deterministic() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    assert bundle.samples == 0
    assert bundle.success_rate is None
    ranked = ModelSelector(RouterConfig()).rank(reg, reqs())
    assert [c.model_id for c in ranked] == ["X"]


# -------------------------------------------- composition + persistence ----


def test_freshness_composes_with_version_isolation() -> None:
    reg = reg_with_ttl()
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=5.0, version="bench-v2"))
    reg.record_benchmark(rec("X", age_s=TTL_S * 10.0, version="bench-v1"))
    # version scoping intact AND freshness applied within each version
    v2 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v2", prefer="benchmark")
    v1 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v1", prefer="benchmark")
    assert v2.samples == 1  # fresh v2 evidence
    assert v1.samples == 0  # stale v1 evidence expired
    assert reg.metrics_versions("X", "ollama") == ["bench-v1", "bench-v2"]


def test_freshness_composes_with_engine_isolation() -> None:
    reg = reg_with_ttl()
    register_worker(reg, engine="ollama")
    reg.register(
        ModelRecord(
            model_id="X",
            engine="llamacpp",  # type: ignore[arg-type]
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    reg.record_benchmark(rec("X", age_s=5.0, engine="ollama"))
    reg.record_benchmark(rec("X", age_s=TTL_S * 10.0, engine="llamacpp"))
    ollama = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    llamacpp = reg.bundle_for("X", "llamacpp", "bug_fix", prefer="benchmark")
    assert ollama.samples == 1  # fresh ollama evidence
    assert llamacpp.samples == 0  # stale llamacpp evidence expired


def test_production_freshness_and_reload() -> None:
    store = _FakeStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store, metric_ttl_s=TTL_S, clock=clock_at())
    register_worker(reg_a)
    reg_a.record_production(
        ProductionSample(
            model_id="X",
            engine="ollama",
            task_class="coding",
            success=True,
            recorded_at=FIXED_NOW - timedelta(seconds=5.0),  # fresh
        )
    )
    reg_a.record_production(
        ProductionSample(
            model_id="X",
            engine="ollama",
            task_class="coding",
            success=False,
            recorded_at=FIXED_NOW - timedelta(seconds=TTL_S * 10.0),  # stale
        )
    )
    # fresh registry over the SAME durable store (simulated restart):
    # the policy is re-applied at aggregation time; legacy records with
    # unknown age are never expired
    reg_b = ModelRegistry(EventLogger(), metric_store=store, metric_ttl_s=TTL_S, clock=clock_at())
    bundle = reg_b.bundle_for("X", "ollama", "coding")
    assert bundle.samples == 1  # ONLY the fresh production sample
    assert bundle.success_rate == 1.0
    # disabled policy (None) keeps both records
    reg_c = ModelRegistry(EventLogger(), metric_store=store, clock=clock_at())
    assert reg_c.bundle_for("X", "ollama", "coding").samples == 2


def test_disabled_ttl_preserves_previous_behavior() -> None:
    reg = reg_with_ttl(ttl=None)
    register_worker(reg)
    reg.record_benchmark(rec("X", age_s=TTL_S * 1000.0))  # ancient: still kept
    assert reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark").samples == 1


class _FakeStore:
    """In-memory ModelMetricStore double (unit tests only, not production)."""

    def __init__(self) -> None:
        self.benchmarks: list[BenchmarkRecord] = []
        self.production: list[ProductionSample] = []

    def save_benchmark(self, record: BenchmarkRecord) -> None:
        self.benchmarks.append(record)

    def save_production(self, sample: ProductionSample) -> None:
        self.production.append(sample)

    def load_benchmarks(self, model_id: str | None = None) -> list[BenchmarkRecord]:
        return [r for r in self.benchmarks if model_id is None or r.model_id == model_id]

    def load_production(self, model_id: str | None = None) -> list[ProductionSample]:
        return [s for s in self.production if model_id is None or s.model_id == model_id]

    def close(self) -> None:
        return None
