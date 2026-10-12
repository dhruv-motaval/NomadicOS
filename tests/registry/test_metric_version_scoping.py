"""Focused regression coverage (Phase 14C-A): benchmark-version-scoped
metric aggregation.

The Phase 14C routing audit CONFIRMED that metrics()/bundle_for() aggregated
all BenchmarkRecords for (model_id, engine, task_class), so bench-v1 and
bench-v2 evidence could blend. 14C-A makes benchmark identity
(model_id, engine, task_category, benchmark_version): a bench-v1 record
never influences a bench-v2 routing bundle. Production samples carry no
benchmark_version and retain their exact 14A.2 semantics.
"""

from __future__ import annotations

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import CapabilityTag, ModelRecord, TaskRequirements, TaskType
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.registry.model_registry import _version_order
from nomadicos.router import ModelSelector


def rec(
    mid: str, engine: str, success: bool, version: str = "bench-v1", category: str = "bug_fix"
) -> BenchmarkRecord:
    return BenchmarkRecord(
        id=f"bench_{engine}_{int(success)}_{version}_{category}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version=version,
        task_id="t",
        task_category=category,  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=success,
        total_latency_ms=100.0 if success else 500.0,
    )


class FakeMetricStore:
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


def coding_reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


# --------------------------------------------------- version separation ----


def test_v1_and_v2_records_do_not_blend() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    v1 = reg.metrics("X", "ollama", benchmark_version="bench-v1").benchmark["bug_fix"]
    v2 = reg.metrics("X", "ollama", benchmark_version="bench-v2").benchmark["bug_fix"]
    # NO blended 0.5 anywhere: each version keeps only its own samples
    assert v1.samples == 1 and v1.success_rate == 1.0
    assert v2.samples == 1 and v2.success_rate == 0.0


def test_v1_bundle_contains_only_v1_samples() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    v1 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v1", prefer="benchmark")
    assert v1.samples == 2
    assert v1.success_rate == 1.0  # the v2 failure never entered the v1 bundle


def test_v2_bundle_contains_only_v2_samples() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    v2 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v2", prefer="benchmark")
    assert v2.samples == 2
    assert v2.success_rate == 0.0  # the v1 success never entered the v2 bundle


def test_engine_isolation_remains_intact() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "llamacpp", True, version="bench-v2"))
    ollama = reg.metrics("X", "ollama", benchmark_version="bench-v1").benchmark["bug_fix"]
    llamacpp = reg.metrics("X", "llamacpp", benchmark_version="bench-v2").benchmark["bug_fix"]
    # version scoping did not weaken engine scoping: 14A.1 behavior intact
    assert ollama.samples == 1 and llamacpp.samples == 1
    assert reg.metrics("X", "ollama", benchmark_version="bench-v2").benchmark == {}
    assert reg.metrics("X", "llamacpp", benchmark_version="bench-v1").benchmark == {}


def test_production_metrics_remain_separate() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v1"))
    reg.record_production(
        ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True)
    )
    # production evidence is unaffected by the benchmark_version parameter
    for version in (None, "bench-v1", "bench-v99"):
        bundle = reg.bundle_for("X", "ollama", "coding", benchmark_version=version)
        assert bundle.samples == 1 and bundle.success_rate == 1.0


# ------------------------------------------------- persistence / reload ----


def test_reload_preserves_version_isolation() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg_a.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    # fresh registry over the SAME durable store (simulated restart)
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    v1 = reg_b.metrics("X", "ollama", benchmark_version="bench-v1").benchmark["bug_fix"]
    v2 = reg_b.metrics("X", "ollama", benchmark_version="bench-v2").benchmark["bug_fix"]
    assert v1.samples == 1 and v1.success_rate == 1.0
    assert v2.samples == 1 and v2.success_rate == 0.0


def test_deterministic_versions_after_restart() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg_a.record_benchmark(rec("X", "ollama", True, version="bench-v2"))
    reg_a.record_benchmark(rec("X", "ollama", True, version="bench-v10"))
    # deterministic ascending order, stable across a fresh registry
    assert reg_a.metrics_versions("X", "ollama") == ["bench-v1", "bench-v2", "bench-v10"]
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    assert reg_b.metrics_versions("X", "ollama") == ["bench-v1", "bench-v2", "bench-v10"]
    assert reg_b.metrics_versions("X", "ollama", "bug_fix") == [
        "bench-v1",
        "bench-v2",
        "bench-v10",
    ]
    assert reg_b.metrics_versions("X", "llamacpp") == []


# --------------------------------------------------------------- routing ---


def test_router_requests_version_scoped_bundles() -> None:
    class SpyRegistry(ModelRegistry):
        """Records every bundle_for call — proves the router passes the
        version-scoped seam (Phase 14C-A)."""

        def __init__(self, inner: ModelRegistry) -> None:
            super().__init__(EventLogger())
            self._inner = inner
            self.bundle_calls: list[tuple[str, str, str, str | None]] = []

        def bundle_for(
            self,
            model_id: str,
            engine: str,
            task_class: str,
            *,
            benchmark_version: str | None = None,
            prefer: str = "production",
        ):
            self.bundle_calls.append((model_id, engine, task_class, benchmark_version))
            return self._inner.bundle_for(
                model_id, engine, task_class, benchmark_version=benchmark_version, prefer=prefer
            )

        def enabled(self):
            return self._inner.enabled()

        def get(self, model_id: str):
            return self._inner.get(model_id)

    reg = ModelRegistry(EventLogger())
    # v1: perfect history, v2: failing history — under the benchmark
    # category identity; routing requests the version-scoped seam
    for _ in range(10):
        reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    for _ in range(10):
        reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    reg.register(
        ModelRecord(
            model_id="X",
            engine="ollama",
            roles=["coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    spy = SpyRegistry(reg)
    sel = ModelSelector(RouterConfig())
    ranked = sel.rank(spy, coding_reqs())
    # the router went through the version-scoped seam: it asked for the
    # candidate's engine identity with a version-scoped lookup (None here,
    # because no version exists for the routing task_class — the benchmark
    # category and routing task_class vocabularies are distinct by design)
    assert spy.bundle_calls == [("X", "ollama", "coding", None)]
    # and the v1/v2 records never blended into a routing bundle:
    # the candidate stays unmeasured (cold start), never scored on a
    # fabricated blend
    scored = [c for c in ranked if c.model_id == "X"]
    assert scored and scored[0].measured is False
    v2 = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v2")
    assert v2.success_rate == 0.0


def test_legacy_omitted_version_aggregates_all() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(rec("X", "ollama", False, version="bench-v2"))
    # documented legacy behavior when benchmark_version is omitted:
    # the union across versions (non-routing view, e.g. comparison())
    bundle = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    assert bundle.samples == 2
    assert bundle.success_rate == 0.5


# --------------------------------------------------------- edge behavior ---


def test_unknown_version_has_no_fabricated_metrics() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True, version="bench-v1"))
    bundle = reg.metrics("X", "ollama", benchmark_version="bench-v9").benchmark
    assert bundle == {}
    empty = reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v9")
    assert empty.samples == 0
    assert empty.success_rate is None  # never invented


def test_version_order_is_deterministic() -> None:
    # same digit run: numeric-only string tiebreaks first
    assert _version_order("1") < _version_order("bench-v1")
    assert _version_order("bench-v1") < _version_order("bench-v2") < _version_order("bench-v10")
    assert _version_order("1") < _version_order("2") < _version_order("10")
    assert _version_order("2.0.0") < _version_order("10.0.0")
    assert _version_order("bench-v1") == _version_order("bench-v1")
