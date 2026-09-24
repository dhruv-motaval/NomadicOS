"""Phase 14A: durable model-metric persistence contracts + registry wiring.

Unit layer: envelope round-trips, fail-closed decode, registry↔store wiring
with an in-memory double, and the DATA-only security boundary. Real
PostgreSQL qualification lives in test_model_metrics_pg.py.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.kernel.events import EventLogger
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable
from nomadicos.persistence.metrics import (
    METRIC_SCHEMA_VERSION,
    decode_benchmark_metric,
    decode_production_metric,
    metric_envelope,
)
from nomadicos.registry import ModelRegistry, ProductionSample


def bench_record(model_id: str = "m1", category: str = "bug_fix") -> BenchmarkRecord:
    return BenchmarkRecord(
        id=f"bench_{model_id}_{category}",
        model_id=model_id,
        engine_id="ollama",
        benchmark_version="bench-v1",
        task_id="t1",
        task_category=category,  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=True,
        total_latency_ms=120.0,
        steps=3,
        retries=0,
    )


class FakeMetricStore:
    """In-memory ModelMetricStore double (unit tests only, not production)."""

    def __init__(self) -> None:
        self.benchmarks: list[BenchmarkRecord] = []
        self.production: list[ProductionSample] = []
        self.fail_save = False

    def save_benchmark(self, record: BenchmarkRecord) -> None:
        if self.fail_save:
            raise PersistenceUnavailable("metric store down")
        self.benchmarks.append(record)

    def save_production(self, sample: ProductionSample) -> None:
        if self.fail_save:
            raise PersistenceUnavailable("metric store down")
        self.production.append(sample)

    def load_benchmarks(self, model_id: str | None = None) -> list[BenchmarkRecord]:
        return [r for r in self.benchmarks if model_id is None or r.model_id == model_id]

    def load_production(self, model_id: str | None = None) -> list[ProductionSample]:
        return [s for s in self.production if model_id is None or s.model_id == model_id]

    def close(self) -> None:
        return None


# ------------------------------------------------------------ envelope --


def test_benchmark_envelope_round_trip() -> None:
    record = bench_record()
    envelope = metric_envelope(record, source="benchmark", engine=record.engine_id)
    assert envelope["schema_version"] == METRIC_SCHEMA_VERSION
    assert envelope["kind"] == "model_metric"
    assert envelope["source"] == "benchmark"
    assert envelope["engine"] == "ollama"
    assert envelope["recorded_at"]
    assert decode_benchmark_metric(envelope) == record


def test_production_envelope_round_trip() -> None:
    sample = ProductionSample(
        model_id="m1", engine="ollama", task_class="coding", success=True, id="prod_m1_coding_0"
    )
    envelope = metric_envelope(sample, source="production", engine=sample.engine)
    assert envelope["source"] == "production"
    assert envelope["engine"] == "ollama"  # sample engine is authoritative
    assert decode_production_metric(envelope) == sample


def test_production_envelope_round_trip_unattributed() -> None:
    sample = ProductionSample(model_id="m1", task_class="coding", success=True, id="prod_x")
    envelope = metric_envelope(sample, source="production")
    assert envelope["engine"] == ""  # explicit UNKNOWN: never invented
    assert decode_production_metric(envelope) == sample


def test_production_decoder_rejects_engine_mismatch() -> None:
    sample = ProductionSample(
        model_id="m1", engine="ollama", task_class="coding", success=True, id="prod_x"
    )
    envelope = metric_envelope(sample, source="production", engine="llamacpp")
    with pytest.raises(PersistenceCorrupt):
        decode_production_metric(envelope)


def test_envelope_preserves_revision_and_quantization_fields() -> None:
    record = bench_record()
    envelope = metric_envelope(
        record,
        source="benchmark",
        engine="ollama",
        revision="2026-09 rev",
        quantization="Q4_K_M",
    )
    assert envelope["revision"] == "2026-09 rev"
    assert envelope["quantization"] == "Q4_K_M"
    assert decode_benchmark_metric(envelope) == record


# ---------------------------------------------------------- fail-closed --


@pytest.mark.parametrize("mutation", ["kind", "version", "source", "body", "fields"])
def test_decode_fails_closed(mutation: str) -> None:
    envelope = metric_envelope(bench_record(), source="benchmark", engine="ollama")
    if mutation == "kind":
        envelope["kind"] = "unknown_kind"
    elif mutation == "version":
        envelope["schema_version"] = METRIC_SCHEMA_VERSION + 1
    elif mutation == "source":
        envelope["source"] = "production"
    elif mutation == "body":
        envelope["record"] = None
    else:
        payload = dict(envelope["record"])
        payload["success"] = "not-a-bool"
        envelope["record"] = payload
    with pytest.raises(PersistenceCorrupt):
        decode_benchmark_metric(envelope)


def test_decode_rejects_non_mapping() -> None:
    with pytest.raises(PersistenceCorrupt):
        decode_benchmark_metric(["not", "a", "mapping"])


def test_production_decoder_rejects_benchmark_source() -> None:
    envelope = metric_envelope(bench_record(), source="benchmark", engine="ollama")
    with pytest.raises(PersistenceCorrupt):
        decode_production_metric(envelope)


# ------------------------------------------------------ registry wiring --


def test_registry_wiring_records_metrics_durably() -> None:
    store = FakeMetricStore()
    reg = ModelRegistry(EventLogger(), metric_store=store)
    reg.record_benchmark(bench_record())
    assert len(store.benchmarks) == 1
    assert reg.metrics("m1", "ollama").benchmark["bug_fix"].samples == 1


def test_registry_wiring_records_production_durably() -> None:
    store = FakeMetricStore()
    reg = ModelRegistry(EventLogger(), metric_store=store)
    reg.record_production(
        ProductionSample(model_id="m1", engine="ollama", task_class="coding", success=True)
    )
    assert len(store.production) == 1
    assert store.production[0].id  # id assigned before durable save
    assert reg.bundle_for("m1", "ollama", "coding").samples == 1


def test_registry_reload_rehydrates_from_store() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(bench_record())
    reg_a.record_production(
        ProductionSample(model_id="m1", engine="ollama", task_class="coding", success=True)
    )
    # fresh registry over the SAME durable store (simulated restart)
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    assert reg_b.metrics("m1", "ollama").benchmark["bug_fix"].samples == 1
    assert reg_b.bundle_for("m1", "ollama", "coding").samples == 1
    assert reg_b.metrics("m1", "ollama").production["coding"].samples == 1


def test_reload_separates_benchmark_from_production() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(bench_record())
    reg_a.record_production(
        ProductionSample(model_id="m1", engine="ollama", task_class="bug_fix", success=False)
    )
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    metrics = reg_b.metrics("m1", "ollama")
    assert metrics.benchmark["bug_fix"].samples == 1
    assert metrics.benchmark["bug_fix"].success_rate == 1.0
    assert metrics.production["bug_fix"].samples == 1
    assert metrics.production["bug_fix"].success_rate == 0.0


def test_registry_without_store_keeps_existing_behavior() -> None:
    reg = ModelRegistry(EventLogger())
    reg.record_benchmark(bench_record())
    assert reg.metric_store is None
    assert reg.metrics("m1", "ollama").benchmark["bug_fix"].samples == 1


def test_metric_store_failure_is_not_hidden() -> None:
    store = FakeMetricStore()
    store.fail_save = True
    reg = ModelRegistry(EventLogger(), metric_store=store)
    with pytest.raises(PersistenceUnavailable):
        reg.record_benchmark(bench_record())


def test_reload_is_deterministic() -> None:
    store = FakeMetricStore()
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_a.record_benchmark(bench_record())
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    reg_c = ModelRegistry(EventLogger(), metric_store=store)
    assert reg_b.metrics("m1", "ollama") == reg_c.metrics("m1", "ollama")


# ------------------------------------------------------------- security --


def test_metrics_module_has_no_authority_or_execution_path() -> None:
    source = inspect.getsource(__import__("nomadicos.persistence.metrics", fromlist=["x"]))
    for token in (
        "AuthorizedAction",
        "grant_full",
        "revoke_all",
        "answer_conflict",
        "add_instruction",
        "TaskStatus.SUCCESS",
        "subprocess",
        "Popen",
        "os.system",
        "urllib",
        "requests",
    ):
        assert token not in source, token


def test_metric_recording_leaves_authority_state_unchanged(tmp_path: Path) -> None:
    from nomadicos.authority.store import AuthorityStore

    authority = AuthorityStore(tmp_path / "authority.json")
    authority.grant_full_pc_autonomy()
    before = authority.state()
    reg = ModelRegistry(EventLogger(), metric_store=FakeMetricStore())
    reg.record_benchmark(bench_record())
    reg.record_production(
        ProductionSample(model_id="m1", engine="ollama", task_class="coding", success=True)
    )
    after = authority.state()
    assert after.epoch == before.epoch
    assert after.grant == before.grant
    assert after.conflicts == before.conflicts
