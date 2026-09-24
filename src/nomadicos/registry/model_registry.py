"""Model registry brick (SPEC §12-13): metadata + measured performance only.

No benchmark numbers are ever invented for production configuration: every
metrics bundle is aggregated from stored measurement records.
"""

from __future__ import annotations

from pydantic import Field

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import MetricsBundle, ModelHealth, ModelMetrics, ModelRecord
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.kernel.events import EventLogger
from nomadicos.persistence.metrics import ModelMetricStore


class ProductionOutcome(Contract):
    """A validated real production outcome (kept distinct from benchmarks)."""

    model_id: str
    task_class: str
    success: bool
    goal_verified: bool | None = None
    tool_success: bool | None = None
    steps: int | None = None
    retries: int | None = None
    latency_s: float | None = None


class ProductionSample(ProductionOutcome):
    id: str = Field(default="")
    #: explicit engine identity (Phase 14A.2). Empty string = unattributed/
    #: unknown — never a guessed engine; unattributed evidence stays in its
    #: own bucket and is never used as exact-engine routing history.
    engine: str = Field(default="")


def _rate(values: list[bool | None]) -> float | None:
    seen = [v for v in values if v is not None]
    if not seen:
        return None
    return round(sum(1 for v in seen if v) / len(seen), 4)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _avg(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def aggregate_benchmarks(records: list[BenchmarkRecord]) -> MetricsBundle:
    """Bundle computed strictly from stored raw measurements (§12)."""
    if not records:
        return MetricsBundle()
    latencies = [r.total_latency_ms / 1000.0 for r in records if r.total_latency_ms is not None]
    return MetricsBundle(
        samples=len(records),
        success_rate=_rate([r.success for r in records]),
        goal_success_rate=_rate([r.goal_verified for r in records]),
        tool_success_rate=_rate([r.tool_success for r in records]),
        repair_success_rate=_rate([r.repair_success for r in records]),
        median_latency_s=_quantile(latencies, 0.5),
        p95_latency_s=_quantile(latencies, 0.95),
        avg_steps=_avg([r.steps for r in records if r.steps is not None]),
        avg_retries=_avg([r.retries for r in records if r.retries is not None]),
    )


def aggregate_production(samples: list[ProductionSample]) -> MetricsBundle:
    if not samples:
        return MetricsBundle()
    latencies = [s.latency_s for s in samples if s.latency_s is not None]
    return MetricsBundle(
        samples=len(samples),
        success_rate=_rate([s.success for s in samples]),
        goal_success_rate=_rate([s.goal_verified for s in samples]),
        tool_success_rate=_rate([s.tool_success for s in samples]),
        median_latency_s=_quantile(latencies, 0.5),
        p95_latency_s=_quantile(latencies, 0.95),
        avg_steps=_avg([s.steps for s in samples if s.steps is not None]),
        avg_retries=_avg([s.retries for s in samples if s.retries is not None]),
    )


class ModelRegistry:
    """Enabled models, their health, and their measured evidence (§12)."""

    def __init__(self, logger: EventLogger, metric_store: ModelMetricStore | None = None) -> None:
        self._logger = logger
        self._records: dict[str, ModelRecord] = {}
        self._benchmark_records: dict[tuple[str, str, str], list[BenchmarkRecord]] = {}
        self._production_samples: dict[tuple[str, str, str], list[ProductionSample]] = {}
        self._metric_store = metric_store
        if metric_store is not None:
            self.reload_metrics()

    @property
    def logger(self) -> EventLogger:
        return self._logger

    @property
    def metric_store(self) -> ModelMetricStore | None:
        """The durable metric store, when wired (Phase 14A)."""
        return self._metric_store

    # ---------------------------------------------------------------- base --
    def register(self, record: ModelRecord) -> ModelRecord:
        self._records[record.model_id] = record
        return record

    def remove(self, model_id: str) -> None:
        self._records.pop(model_id, None)

    def get(self, model_id: str) -> ModelRecord:
        try:
            return self._records[model_id]
        except KeyError:
            raise ResourceUnavailable(f"model {model_id!r} not registered") from None

    def all(self) -> list[ModelRecord]:
        return sorted(self._records.values(), key=lambda r: r.model_id)

    def enabled(self) -> list[ModelRecord]:
        return [r for r in self.all() if r.enabled]

    def set_health(self, model_id: str, health: ModelHealth) -> None:
        self.get(model_id).health = health

    # ------------------------------------------------------------ metrics --
    def record_benchmark(self, record: BenchmarkRecord) -> None:
        """Append-only (SPEC §52C: never silently rewrite history). Records
        are stored under engine-scoped identity (model_id, engine,
        task_category) — Phase 14A.1: the same model_id + task_category
        under different engines never share a bundle. When a durable metric
        store is wired the record persists too — persistence errors
        surface, they are never hidden (Phase 14A)."""
        key: tuple[str, str, str] = (record.model_id, record.engine_id, record.task_category)
        self._benchmark_records.setdefault(key, []).append(record)
        if self._metric_store is not None:
            self._metric_store.save_benchmark(record)

    def record_production(self, sample: ProductionSample) -> None:
        # production samples carry explicit engine identity (Phase 14A.2);
        # unattributed samples (engine "") stay in their own bucket and are
        # never blended into exact-engine routing history
        key: tuple[str, str, str] = (sample.model_id, sample.engine, sample.task_class)
        stored = sample.model_copy(
            update={"id": f"prod_{key[0]}_{key[2]}_{len(self._production_samples.get(key, []))}"}
        )
        self._production_samples.setdefault(key, []).append(stored)
        if self._metric_store is not None:
            self._metric_store.save_production(stored)

    def reload_metrics(self) -> None:
        """Rehydrate in-memory metric samples from the durable store
        (Phase 14A): metrics survive restart; unmeasured models stay
        ``samples == 0``. Corrupt or incompatible records fail closed.
        Both sources reconstruct under engine-scoped identity (Phase
        14A.1/14A.2); legacy unattributed records keep engine ""."""
        if self._metric_store is None:
            return
        benchmarks = self._metric_store.load_benchmarks()
        production = self._metric_store.load_production()
        self._benchmark_records = {}
        self._production_samples = {}
        for record in benchmarks:
            key: tuple[str, str, str] = (record.model_id, record.engine_id, record.task_category)
            self._benchmark_records.setdefault(key, []).append(record)
        for sample in production:
            # legacy unattributed records keep engine "" (no guessed engine);
            # they reconstruct into their own (model_id, "", task_class) bucket
            key2: tuple[str, str, str] = (sample.model_id, sample.engine, sample.task_class)
            self._production_samples.setdefault(key2, []).append(sample)

    def metrics(self, model_id: str, engine: str) -> ModelMetrics:
        """Engine-scoped bundles (Phase 14A.1/14A.2): the same model_id +
        task_class under different engines never blend. Unattributed
        production records (engine "") stay in their own bucket and are
        never returned as exact-engine history."""
        benchmark: dict[str, MetricsBundle] = {}
        production: dict[str, MetricsBundle] = {}
        for (mid, eng, cls), records in self._benchmark_records.items():
            if mid == model_id and eng == engine:
                benchmark[cls] = aggregate_benchmarks(records)
        for (mid, eng, cls), samples in self._production_samples.items():
            if mid == model_id and eng == engine:
                production[cls] = aggregate_production(samples)
        return ModelMetrics(model_id=model_id, benchmark=benchmark, production=production)

    def bundle_for(
        self, model_id: str, engine: str, task_class: str, *, prefer: str = "production"
    ) -> MetricsBundle:
        """Resolve the EXACT engine-scoped bundle (Phase 14A.1/14A.2).
        Production evidence preferred, benchmark as fallback (never mixed);
        both sources are engine-exact — no cross-engine blending, no
        cross-engine fallback, no unattributed contamination."""
        metrics = self.metrics(model_id, engine)
        if prefer == "production":
            return (
                metrics.production.get(task_class)
                or metrics.benchmark.get(task_class)
                or MetricsBundle()
            )
        return (
            metrics.benchmark.get(task_class)
            or metrics.production.get(task_class)
            or MetricsBundle()
        )
