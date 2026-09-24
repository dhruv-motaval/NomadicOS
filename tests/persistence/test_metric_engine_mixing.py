"""Focused regression coverage (Phase 14A.1): engine-scoped metric
aggregation.

The Phase 14A audit CONFIRMED the pre-existing bug where in-memory
aggregation merged records from different engines under the same
model_id + task_category, and the blended bundle fed ``ModelSelector.rank``
via ``bundle_for`` on the CURRENT routing path. 14A.1 fixes the
router-facing view: benchmark metric identity is (model_id, engine,
task_category); production samples remain engine-unattributed (the source
record has no engine field — no invented values). The durable PostgreSQL
layer already preserved engine identity per record and is unchanged.
"""

from __future__ import annotations

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import CapabilityTag, ModelRecord, TaskRequirements, TaskType
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.router import ModelSelector


def rec(mid: str, engine: str, success: bool, category: str = "bug_fix") -> BenchmarkRecord:
    return BenchmarkRecord(
        id=f"bench_{engine}_{int(success)}_{category}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version="bench-v1",
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


# ------------------------------------------------- A. separate bundles -----


def test_engine_bundles_are_separate() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True))
    reg.record_benchmark(rec("X", "llamacpp", False))
    ollama = reg.metrics("X", "ollama").benchmark["bug_fix"]
    llamacpp = reg.metrics("X", "llamacpp").benchmark["bug_fix"]
    assert ollama.samples == 1
    assert ollama.success_rate == 1.0
    assert llamacpp.samples == 1
    assert llamacpp.success_rate == 0.0
    # neither bundle contains the other engine's sample
    assert ollama.samples + llamacpp.samples == 2


# ------------------------------------ B. independent success rates ---------


def test_success_rates_remain_independent() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True))
    reg.record_benchmark(rec("X", "llamacpp", False))
    ollama = reg.bundle_for("X", "ollama", "bug_fix", prefer="benchmark")
    llamacpp = reg.bundle_for("X", "llamacpp", "bug_fix", prefer="benchmark")
    # NO blended 0.5 result anywhere
    assert ollama.success_rate == 1.0
    assert llamacpp.success_rate == 0.0


class SpyRegistry(ModelRegistry):
    """ModelRegistry wrapper recording every ``bundle_for`` call — proves
    which engine the router requests on the CURRENT routing path."""

    def __init__(self, inner: ModelRegistry) -> None:
        super().__init__(EventLogger())
        self._inner = inner
        self.bundle_calls: list[tuple[str, str, str]] = []

    def bundle_for(
        self, model_id: str, engine: str, task_class: str, *, prefer: str = "production"
    ):
        self.bundle_calls.append((model_id, engine, task_class))
        return self._inner.bundle_for(model_id, engine, task_class, prefer=prefer)

    def enabled(self):
        return self._inner.enabled()

    def get(self, model_id: str):
        return self._inner.get(model_id)


# ---------------------------- C. router consumes exact engine history ------


def test_router_consumes_exact_engine_history() -> None:
    reg = ModelRegistry(EventLogger())
    # X: weak llamacpp history, strong ollama history — engine-scoped and
    # never blended (asserted directly by the bundle tests above)
    for _ in range(10):
        reg.record_benchmark(rec("X", "llamacpp", False))
    for _ in range(10):
        reg.record_benchmark(rec("X", "ollama", True))
    # production evidence drives the router's current consumption path —
    # attributed to the candidate's engine (Phase 14A.2)
    for _ in range(10):
        reg.record_production(
            ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True)
        )
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
    # the router requested the candidate's ACTUAL engine — ("X", "ollama",
    # "coding"), never the old engine-less ("X", "coding") lookup
    assert spy.bundle_calls == [("X", "ollama", "coding")]
    scored = [c for c in ranked if c.model_id == "X"]
    assert scored and scored[0].measured is True
    assert scored[0].score == 1.0  # own (production) evidence only
    assert sel.select(spy, coding_reqs()).selected.model_id == "X"


# ------------------------------------------- D. no cross-engine fallback ---


def test_no_cross_engine_fallback() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(rec("X", "ollama", True))  # ollama has history
    # X + llamacpp has NO history: must NOT receive ollama's evidence
    bundle = reg.bundle_for("X", "llamacpp", "bug_fix", prefer="benchmark")
    assert bundle.samples == 0
    assert bundle.success_rate is None


# --------------------------------------- E. restart/reload preservation ----


def test_reload_preserves_engine_scoped_bundles() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(rec("X", "ollama", True))
    reg_a.record_benchmark(rec("X", "llamacpp", False))
    # fresh registry over the SAME durable store (simulated restart)
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    ollama = reg_b.metrics("X", "ollama").benchmark["bug_fix"]
    llamacpp = reg_b.metrics("X", "llamacpp").benchmark["bug_fix"]
    assert ollama.samples == 1 and ollama.success_rate == 1.0
    assert llamacpp.samples == 1 and llamacpp.success_rate == 0.0
    # distinct model ids still do not merge (existing behavior intact)
    reg_b.record_benchmark(rec("Y", "llamacpp", True))
    assert reg_b.metrics("X", "llamacpp").benchmark["bug_fix"].samples == 1
    assert reg_b.metrics("Y", "llamacpp").benchmark["bug_fix"].samples == 1


# =========================== PHASE 14A.2: PRODUCTION ENGINE IDENTITY ========


def prod(mid: str, engine: str, success: bool, task_class: str = "coding") -> ProductionSample:
    return ProductionSample(
        model_id=mid,
        engine=engine,
        task_class=task_class,
        success=success,
        latency_s=1.0 if success else 5.0,
    )


# ------------------------------------ A. production engine separation ------


def test_production_engine_separation() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_production(prod("X", "ollama", True))
    reg.record_production(prod("X", "llamacpp", False))
    ollama = reg.bundle_for("X", "ollama", "coding")
    llamacpp = reg.bundle_for("X", "llamacpp", "coding")
    # NO blended 0.5 anywhere
    assert ollama.samples == 1 and ollama.success_rate == 1.0
    assert llamacpp.samples == 1 and llamacpp.success_rate == 0.0


# -------------------------- B. router exact-engine production history ------


def test_router_consumes_exact_engine_production_history() -> None:
    reg = ModelRegistry(EventLogger())
    # X was served by llamacpp (weak production) before ollama (strong):
    # the candidate's registered engine selects ONLY its own history
    for _ in range(10):
        reg.record_production(prod("X", "llamacpp", False))
    for _ in range(10):
        reg.record_production(prod("X", "ollama", True))
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
    # the router requested the candidate's ACTUAL engine — and with only
    # production evidence present, the ollama production bundle drove the
    # outcome (pre-14A.1/14A.2 a blended 0.5 failed the BALANCED quality
    # gate and the candidate was excluded entirely)
    assert spy.bundle_calls == [("X", "ollama", "coding")]
    scored = [c for c in ranked if c.model_id == "X"]
    assert scored, "candidate must qualify on its own engine's production history"
    assert scored[0].measured is True
    assert scored[0].score == 1.0  # ollama production evidence only
    assert sel.select(spy, coding_reqs()).selected.model_id == "X"


# ------------------------------------ C. no cross-engine fallback ----------


def test_no_cross_engine_production_fallback() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_production(prod("X", "ollama", True))  # ollama has history
    # X + llamacpp has NO production history: must NOT receive ollama's
    bundle = reg.bundle_for("X", "llamacpp", "coding")
    assert bundle.samples == 0
    assert bundle.success_rate is None


# -------------------------- D. unknown/legacy engine isolation --------------


def test_unattributed_production_is_not_exact_engine_history() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_production(prod("X", "", True))  # legacy/unattributed
    # unattributed evidence is never treated as exact-engine history
    assert reg.bundle_for("X", "ollama", "coding").samples == 0
    assert reg.bundle_for("X", "llamacpp", "coding").samples == 0
    # it remains queryable under its own explicit identity (non-routing view)
    legacy = reg.metrics("X", "").production["coding"]
    assert legacy.samples == 1 and legacy.success_rate == 1.0


# ------------------------------------------ E. restart/reload ---------------


def test_reload_preserves_production_engine_identity() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_production(prod("X", "ollama", True))
    reg_a.record_production(prod("X", "llamacpp", False))
    reg_a.record_production(prod("X", "", True))  # legacy/unattributed
    # fresh registry over the SAME durable store (simulated restart)
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    assert reg_b.bundle_for("X", "ollama", "coding").samples == 1
    assert reg_b.bundle_for("X", "llamacpp", "coding").samples == 1
    # all three remain distinct: ollama/llamacpp/"" buckets never merge
    assert reg_b.metrics("X", "ollama").production["coding"].samples == 1
    assert reg_b.metrics("X", "llamacpp").production["coding"].samples == 1
    assert reg_b.metrics("X", "").production["coding"].samples == 1
    assert reg_b.metrics("X", "").production["coding"].success_rate == 1.0
    assert reg_b.metrics("X", "llamacpp").production["coding"].success_rate == 0.0


# ------------------------------------------ G. multiple samples same engine -


def test_multiple_samples_same_engine_aggregate() -> None:
    reg = ModelRegistry(EventLogger())
    for _ in range(3):
        reg.record_production(prod("X", "ollama", True))
    for _ in range(1):
        reg.record_production(prod("X", "ollama", False))
    reg.record_production(prod("X", "llamacpp", False))  # other engine: isolated
    bundle = reg.bundle_for("X", "ollama", "coding")
    assert bundle.samples == 4  # ONLY the ollama samples aggregated
    assert bundle.success_rate == 0.75
    assert reg.bundle_for("X", "llamacpp", "coding").samples == 1
