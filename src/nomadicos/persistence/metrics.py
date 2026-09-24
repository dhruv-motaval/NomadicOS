"""Durable model-metric persistence (Phase 14A, SPEC §34 model_metrics).

Sample-level metric evidence is stored as versioned, typed DATA records.
Metrics are DATA ONLY: they never grant authority, execute tools, decide
verification, or route (SPEC §33, §52C — learning/evaluation stays outside
the authority path).

Every durable record carries the full runtime/model identity — model_id,
engine, benchmark_version, task category, plus revision/quantization fields
that stay empty until the runtime can actually provide them (Phase 14A
discovery: keying by model_id + task_category alone can mix incompatible
engine/version evidence). The raw sample record (BenchmarkRecord or
ProductionSample) is preserved verbatim so aggregates can always be
deterministically reconstructed from stored measurements.

One narrow typed interface with a PostgreSQL implementation (production
durability). In-memory doubles in the test suite exercise the registry
wiring without a database; they are not production adapters.

Staleness: every record carries `recorded_at` (and benchmark records carry
their measurement `timestamp`). No age/TTL policy is defined yet — the data
is capable of detecting staleness; the policy itself is unresolved and is
NOT invented here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.persistence.contracts import now_iso
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable

if TYPE_CHECKING:  # typing aid only (avoids an import cycle with the registry)
    from nomadicos.registry.model_registry import ProductionSample

#: minimum schema version written/read by this build; other versions fail
#: closed on load (no auto-migration, matching the task-state pattern)
METRIC_SCHEMA_VERSION = 1

MetricSource = Literal["benchmark", "production"]


def metric_envelope(
    record: BenchmarkRecord | ProductionSample,
    *,
    source: MetricSource,
    engine: str = "",
    revision: str = "",
    quantization: str = "",
) -> dict[str, Any]:
    """Versioned serialization envelope carrying the full model/runtime
    identity plus the verbatim sample record (provenance, SPEC §52C)."""
    return {
        "schema_version": METRIC_SCHEMA_VERSION,
        "kind": "model_metric",
        "source": source,
        "engine": engine,
        "revision": revision,
        "quantization": quantization,
        "recorded_at": now_iso(),
        "record": record.model_dump(mode="json"),
    }


def _decode_metric(raw: Any, *, expected_source: MetricSource) -> dict[str, Any]:
    """Fail-closed envelope checks shared by both decoders: kind, version,
    source, and shape are re-validated on load — persisted metric data is
    never trusted as-is."""
    if not isinstance(raw, dict):
        raise PersistenceCorrupt("metric record is not a mapping")
    if raw.get("kind") != "model_metric":
        raise PersistenceCorrupt(f"unknown metric record kind {raw.get('kind')!r}")
    if raw.get("schema_version") != METRIC_SCHEMA_VERSION:
        raise PersistenceCorrupt(
            f"metric schema version mismatch: stored {raw.get('schema_version')!r}, "
            f"this build reads {METRIC_SCHEMA_VERSION}"
        )
    if raw.get("source") != expected_source:
        raise PersistenceCorrupt(
            f"metric source mismatch: expected {expected_source!r}, stored {raw.get('source')!r}"
        )
    payload = raw.get("record")
    if not isinstance(payload, dict):
        raise PersistenceCorrupt("missing metric record body")
    return payload


def decode_benchmark_metric(raw: Any) -> BenchmarkRecord:
    """Fail-closed decode of one durable benchmark metric envelope."""
    payload = _decode_metric(raw, expected_source="benchmark")
    try:
        return BenchmarkRecord.model_validate(payload)
    except Exception as exc:
        raise PersistenceCorrupt(f"benchmark metric record failed validation: {exc}") from exc


def decode_production_metric(raw: Any) -> ProductionSample:
    """Fail-closed decode of one durable production metric envelope."""
    from nomadicos.registry.model_registry import ProductionSample

    payload = _decode_metric(raw, expected_source="production")
    if raw.get("engine") != payload.get("engine", ""):
        # envelope engine and sample engine are written together at save
        # time; a mismatch means corrupted/attribution-tampered evidence
        raise PersistenceCorrupt(
            f"production metric engine mismatch: envelope {raw.get('engine')!r}, "
            f"sample {payload.get('engine')!r}"
        )
    try:
        return ProductionSample.model_validate(payload)
    except Exception as exc:
        raise PersistenceCorrupt(f"production metric record failed validation: {exc}") from exc


class ModelMetricStore(Protocol):
    """Durable model-metric store (SPEC §34 model_metrics). DATA boundary
    only: it never authorizes, executes, routes, or decides verification."""

    def save_benchmark(self, record: BenchmarkRecord) -> None: ...

    def save_production(self, sample: ProductionSample) -> None: ...

    def load_benchmarks(self, model_id: str | None = None) -> list[BenchmarkRecord]: ...

    def load_production(self, model_id: str | None = None) -> list[ProductionSample]: ...

    def close(self) -> None: ...


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nomadic_model_metrics (
    metric_id      TEXT PRIMARY KEY,
    model_id       TEXT NOT NULL,
    source         TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    record         JSONB NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_MODEL_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_nomadic_model_metrics_model
ON nomadic_model_metrics (model_id, source)
"""

_SAVE_SQL = """
INSERT INTO nomadic_model_metrics (metric_id, model_id, source, schema_version, record)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (metric_id) DO NOTHING
"""


class PostgresModelMetricStore:
    """PostgreSQL adapter: durable source of truth for model metrics
    (Phase 14A). Same narrow typed boundary and fail-closed semantics as
    the task-state store. Append-only: a duplicate metric_id is idempotent
    (ON CONFLICT DO NOTHING) — measurement history is never silently
    rewritten (SPEC §52C)."""

    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
        import psycopg

        self._psycopg = psycopg
        try:
            self._conn = psycopg.connect(
                dsn, autocommit=True, connect_timeout=max(1, int(connect_timeout_s))
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"PostgreSQL unavailable: {exc}") from exc
        try:
            self._conn.execute(_SCHEMA_SQL)
            self._conn.execute(_MODEL_INDEX_SQL)
        except Exception as exc:
            raise PersistenceUnavailable(f"metric schema initialization failed: {exc}") from exc

    def save_benchmark(self, record: BenchmarkRecord) -> None:
        self._save(record, source="benchmark", engine=record.engine_id)

    def save_production(self, sample: ProductionSample) -> None:
        # the sample's engine is the authoritative identity ("" = explicit
        # unattributed/unknown — never a guessed engine, Phase 14A.2)
        self._save(sample, source="production", engine=sample.engine)

    def load_benchmarks(self, model_id: str | None = None) -> list[BenchmarkRecord]:
        return [
            decode_benchmark_metric(row[0])
            for row in self._load(source="benchmark", model_id=model_id)
        ]

    def load_production(self, model_id: str | None = None) -> list[ProductionSample]:
        return [
            decode_production_metric(row[0])
            for row in self._load(source="production", model_id=model_id)
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            return None

    # ------------------------------------------------------------ helper --
    def _save(
        self, record: BenchmarkRecord | ProductionSample, *, source: MetricSource, engine: str
    ) -> None:
        from psycopg.types.json import Jsonb

        if not record.id:
            raise PersistenceCorrupt("metric sample requires a non-empty id")
        envelope = metric_envelope(record, source=source, engine=engine)
        try:
            self._conn.execute(
                _SAVE_SQL,
                (record.id, record.model_id, source, METRIC_SCHEMA_VERSION, Jsonb(envelope)),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"metric save failed: {exc}") from exc

    def _load(self, *, source: MetricSource, model_id: str | None = None) -> list[tuple[Any, ...]]:
        try:
            if model_id is None:
                rows = self._conn.execute(
                    "SELECT record FROM nomadic_model_metrics WHERE source = %s ORDER BY metric_id",
                    (source,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT record FROM nomadic_model_metrics "
                    "WHERE source = %s AND model_id = %s ORDER BY metric_id",
                    (source, model_id),
                ).fetchall()
        except Exception as exc:
            raise PersistenceUnavailable(f"metric load failed: {exc}") from exc
        return rows
