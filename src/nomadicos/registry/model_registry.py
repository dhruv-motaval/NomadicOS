"""Model registry brick (SPEC §12-13): metadata + measured performance only.

No benchmark numbers are ever invented for production configuration: every
metrics bundle is aggregated from stored measurement records.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

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
    #: actual write time stamped by the registry clock (Phase 14C-C);
    #: None = legacy record with unknown age — never expired, never invented
    recorded_at: datetime | None = None


def _rate(values: list[bool | None]) -> float | None:
    seen = [v for v in values if v is not None]
    if not seen:
        return None
    return round(sum(1 for v in seen if v) / len(seen), 4)


def _version_order(version: str) -> tuple[tuple[int, ...], str]:
    """Deterministic benchmark-version order (Phase 14C-A): digit runs
    compared element-wise ("1" < "2" < "10", "bench-v2" < "bench-v10");
    versionless strings sort first; ties by the full string. Pure ordering
    - never a staleness or revision policy."""
    import re

    nums = tuple(int(m) for m in re.findall(r"\d+", version))
    return (nums, version)


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
    """Enabled models, their health, and their measured evidence (§12).

    Registry identity is engine-aware (Phase 14C-F): records are keyed by
    ``(model_id, engine)``, so the same model_id can be registered under
    several engines without overwriting. Bare model_id lookups resolve
    deterministically: exactly one record returns it; several raise
    (fail-closed, never an arbitrary winner)."""

    def __init__(
        self,
        logger: EventLogger,
        metric_store: ModelMetricStore | None = None,
        *,
        metric_ttl_s: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._logger = logger
        #: (model_id, engine) -> record (Phase 14C-F engine-aware identity)
        self._records: dict[tuple[str, str], ModelRecord] = {}
        #: (model_id, engine, task_category, benchmark_version) -> records
        #: (Phase 14C-A): benchmark identity is version-scoped — a bench-v1
        #: record never blends into a bench-v2 bundle
        self._benchmark_records: dict[tuple[str, str, str, str], list[BenchmarkRecord]] = {}
        self._production_samples: dict[tuple[str, str, str], list[ProductionSample]] = {}
        self._metric_store = metric_store
        #: freshness policy (Phase 14C-C): None = disabled (all evidence
        #: eligible); otherwise records older than the TTL are excluded
        #: from every aggregation — expired evidence cannot dominate fresh
        self._metric_ttl_s = metric_ttl_s
        #: injectable clock (tests use a fixed one; never a fabricated time)
        self._clock = clock or (lambda: datetime.now(UTC))
        if metric_store is not None:
            self.reload_metrics()

    @property
    def logger(self) -> EventLogger:
        return self._logger

    @property
    def metric_store(self) -> ModelMetricStore | None:
        """The durable metric store, when wired (Phase 14A)."""
        return self._metric_store

    def wire_metric_store(self, store: ModelMetricStore) -> None:
        """Attach the durable metric store at runtime (Phase 14B benchmark
        seam): benchmark records persist through the same engine-scoped
        store; existing metrics are rehydrated immediately."""
        self._metric_store = store
        self.reload_metrics()

    # ---------------------------------------------------------------- base --
    def register(self, record: ModelRecord) -> ModelRecord:
        self._records[(record.model_id, record.engine)] = record
        return record

    def register_dir(self, record: ModelRecord) -> ModelRecord:
        """Register a models-dir scan record WITHOUT downgrading health.

        A file on disk is not proof of serving availability (SPEC §12): the
        dir scan is a fallback that must never overwrite a record whose live
        engine health is already known. HEALTHY/DEGRADED stay authoritative;
        UNKNOWN is only written when no live record exists for this identity.
        """
        key = (record.model_id, record.engine)
        existing = self._records.get(key)
        if existing is not None and existing.health in (ModelHealth.HEALTHY, ModelHealth.DEGRADED):
            return existing
        self._records[key] = record
        return record

    def remove(self, model_id: str, engine: str | None = None) -> None:
        """Exact engine identity when given; a bare model_id removes every
        engine record for it (deterministic sweep)."""
        if engine is not None:
            self._records.pop((model_id, engine), None)
            return
        for key in [k for k in self._records if k[0] == model_id]:
            del self._records[key]

    def get(self, model_id: str, engine: str | None = None) -> ModelRecord:
        if engine is not None:
            try:
                return self._records[(model_id, engine)]
            except KeyError:
                raise ResourceUnavailable(
                    f"model {model_id!r} not registered on engine {engine!r}"
                ) from None
        matches = [r for (mid, _eng), r in self._records.items() if mid == model_id]
        if not matches:
            raise ResourceUnavailable(f"model {model_id!r} not registered") from None
        if len(matches) > 1:
            raise ResourceUnavailable(
                f"model {model_id!r} is registered under multiple engines; specify engine"
            ) from None
        return matches[0]

    def all(self) -> list[ModelRecord]:
        return sorted(self._records.values(), key=lambda r: (r.model_id, r.engine))

    def enabled(self) -> list[ModelRecord]:
        return [r for r in self.all() if r.enabled]

    def set_health(self, model_id: str, health: ModelHealth, engine: str | None = None) -> None:
        self.get(model_id, engine).health = health

    # ------------------------------------------------------------ metrics --
    def record_benchmark(self, record: BenchmarkRecord) -> None:
        """Append-only (SPEC §52C: never silently rewrite history). Records
        are stored under engine-scoped identity (model_id, engine,
        task_category) — Phase 14A.1: the same model_id + task_category
        under different engines never share a bundle — and version-scoped
        identity (Phase 14C-A): the same model_id + engine + task_category
        under different benchmark_versions never share a bundle. When a
        durable metric store is wired the record persists too — persistence
        errors surface, they are never hidden (Phase 14A)."""
        key: tuple[str, str, str, str] = (
            record.model_id,
            record.engine_id,
            record.task_category,
            record.benchmark_version,
        )
        self._benchmark_records.setdefault(key, []).append(record)
        if self._metric_store is not None:
            self._metric_store.save_benchmark(record)

    def record_production(self, sample: ProductionSample) -> None:
        # production samples carry explicit engine identity (Phase 14A.2);
        # unattributed samples (engine "") stay in their own bucket and are
        # never blended into exact-engine routing history. The write time is
        # recorded from the actual clock (Phase 14C-C) — never fabricated.
        key: tuple[str, str, str] = (sample.model_id, sample.engine, sample.task_class)
        stored = sample.model_copy(
            update={
                "id": f"prod_{key[0]}_{key[2]}_{len(self._production_samples.get(key, []))}",
                "recorded_at": sample.recorded_at or self._clock(),
            }
        )
        self._production_samples.setdefault(key, []).append(stored)
        if self._metric_store is not None:
            self._metric_store.save_production(stored)

    def reload_metrics(self) -> None:
        """Rehydrate in-memory metric samples from the durable store
        (Phase 14A): metrics survive restart; unmeasured models stay
        ``samples == 0``. Corrupt or incompatible records fail closed.
        Both sources reconstruct under engine-scoped identity (Phase
        14A.1/14A.2); legacy unattributed records keep engine "". Benchmark
        records reconstruct under version-scoped identity (Phase 14C-A)."""
        if self._metric_store is None:
            return
        benchmarks = self._metric_store.load_benchmarks()
        production = self._metric_store.load_production()
        self._benchmark_records = {}
        self._production_samples = {}
        for record in benchmarks:
            key: tuple[str, str, str, str] = (
                record.model_id,
                record.engine_id,
                record.task_category,
                record.benchmark_version,
            )
            self._benchmark_records.setdefault(key, []).append(record)
        for sample in production:
            # legacy unattributed records keep engine "" (no guessed engine);
            # they reconstruct into their own (model_id, "", task_class) bucket
            key2: tuple[str, str, str] = (sample.model_id, sample.engine, sample.task_class)
            self._production_samples.setdefault(key2, []).append(sample)

    def metrics(
        self, model_id: str, engine: str, *, benchmark_version: str | None = None
    ) -> ModelMetrics:
        """Engine-scoped bundles (Phase 14A.1/14A.2): the same model_id +
        task_class under different engines never blend. Unattributed
        production records (engine "") stay in their own bucket and are
        never returned as exact-engine history.

        Version-scoped benchmark evidence (Phase 14C-A): an explicit
        ``benchmark_version`` aggregates ONLY that version's records. When
        omitted, the documented legacy aggregate (all versions combined)
        is preserved — non-routing views; routing itself always requests
        the newest recorded version explicitly, so a bench-v1 record never
        influences a bench-v2 routing bundle. Production samples carry no
        benchmark_version and are unaffected by this parameter.

        Freshness (Phase 14C-C): when the registry TTL policy is configured,
        records older than ``now - metric_ttl_s`` are excluded from every
        aggregation (expired evidence cannot dominate fresh evidence); a
        record with unknown age (None timestamp) is never expired. All
        evidence stale ⇒ an empty bundle ⇒ deterministic cold start."""
        now = self._clock()
        benchmark: dict[str, MetricsBundle] = {}
        production: dict[str, MetricsBundle] = {}
        benchmark_raw: dict[str, list[BenchmarkRecord]] = {}
        for (mid, eng, cls, ver), records in self._benchmark_records.items():
            if (
                mid == model_id
                and eng == engine
                and (benchmark_version is None or ver == benchmark_version)
            ):
                benchmark_raw.setdefault(cls, []).extend(self._fresh(records, now, "timestamp"))
        for cls, records in benchmark_raw.items():
            benchmark[cls] = aggregate_benchmarks(records)
        for (mid, eng, cls), samples in self._production_samples.items():
            if mid == model_id and eng == engine:
                production[cls] = aggregate_production(self._fresh(samples, now, "recorded_at"))
        return ModelMetrics(model_id=model_id, benchmark=benchmark, production=production)

    def _fresh(self, records: list[Any], now: datetime, field: str) -> list[Any]:
        """Freshness filter (Phase 14C-C): drop records older than the TTL
        measured against the actual clock; unknown age (None) stays —
        unknown is never treated as stale and never invented."""
        if self._metric_ttl_s is None:
            return records
        cutoff = now - timedelta(seconds=self._metric_ttl_s)
        return [
            r for r in records if getattr(r, field, None) is None or getattr(r, field) >= cutoff
        ]

    def metrics_versions(
        self, model_id: str, engine: str, task_class: str | None = None
    ) -> list[str]:
        """Distinct benchmark versions present for the engine-scoped
        identity (Phase 14C-A), deterministic ascending order. Routing uses
        the last entry (newest recorded version) explicitly."""
        versions = {
            key[3]
            for key in self._benchmark_records
            if key[0] == model_id
            and key[1] == engine
            and (task_class is None or key[2] == task_class)
        }
        return sorted(versions, key=_version_order)

    def bundle_for(
        self,
        model_id: str,
        engine: str,
        task_class: str,
        *,
        benchmark_version: str | None = None,
        prefer: str = "production",
    ) -> MetricsBundle:
        """Resolve the EXACT engine-scoped bundle (Phase 14A.1/14A.2).
        Production evidence preferred, benchmark as fallback (never mixed);
        both sources are engine-exact — no cross-engine blending, no
        cross-engine fallback, no unattributed contamination.

        ``benchmark_version`` (Phase 14C-A) scopes the benchmark fallback to
        that exact version; when omitted the legacy aggregate (all versions)
        is used. Production evidence is unaffected by the version."""
        metrics = self.metrics(model_id, engine, benchmark_version=benchmark_version)
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
