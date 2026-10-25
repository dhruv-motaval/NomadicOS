"""Phase 14A: REAL PostgreSQL durable model-metric persistence qualification.

The full durability flow with PostgreSQL as the production durable store for
model metrics (SPEC §34, Phase 14A):

- save/load round-trip (benchmark + production samples, verbatim provenance)
- model / task-category / benchmark-vs-production / engine / benchmark-version
  identity isolation
- restart preserves metrics (fresh store over the same database; registry
  reload rehydrates)
- deterministic aggregation after reload
- samples == 0 never invents rates
- corrupt record / schema mismatch / source mismatch fail closed
- unavailable PostgreSQL raises structured PersistenceUnavailable
- duplicate writes are idempotent (append-only, ON CONFLICT DO NOTHING)
- authority state is untouched by metric persistence

Requires the docker-compose.dev.yml PostgreSQL (dev credentials; see
NOMADICOS_TEST_DSN). Each test uses unique model ids so PG-persisted rows
from other tests never contaminate results.
"""

from __future__ import annotations

import os
import uuid

import pytest

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.kernel.events import EventLogger
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable
from nomadicos.persistence.metrics import PostgresModelMetricStore
from nomadicos.registry import ModelRegistry, ProductionSample, aggregate_benchmarks

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


def unique_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def bench_record(
    model_id: str, category: str = "bug_fix", engine: str = "ollama"
) -> BenchmarkRecord:
    return BenchmarkRecord(
        id=unique_id("bench"),
        model_id=model_id,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version="bench-v1",
        task_id=unique_id("benchtask"),
        task_category=category,  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=True,
        total_latency_ms=120.0,
        steps=3,
        retries=0,
    )


# ------------------------------------------------------- round-trip --------


def test_metric_save_load_round_trip() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    bench = bench_record(mid)
    sample = ProductionSample(
        model_id=mid, engine="ollama", task_class="coding", success=True, id=unique_id("prod")
    )
    store.save_benchmark(bench)
    store.save_production(sample)
    loaded_bench = store.load_benchmarks(mid)
    loaded_prod = store.load_production(mid)
    assert loaded_bench == [bench]
    assert loaded_prod == [sample]
    store.close()


def test_production_sample_without_id_is_rejected() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    with pytest.raises(PersistenceCorrupt):
        store.save_production(
            ProductionSample(model_id=unique_id("m"), task_class="coding", success=True)
        )
    store.close()


# --------------------------------------------------------- isolation -------


def test_models_remain_isolated() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    m1, m2 = unique_id("m"), unique_id("m")
    store.save_benchmark(bench_record(m1))
    store.save_benchmark(bench_record(m2))
    assert [r.model_id for r in store.load_benchmarks(m1)] == [m1]
    assert [r.model_id for r in store.load_benchmarks(m2)] == [m2]
    store.close()


def test_task_categories_remain_isolated() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    store.save_benchmark(bench_record(mid, category="bug_fix"))
    store.save_benchmark(bench_record(mid, category="repair"))
    reg = ModelRegistry(EventLogger(), metric_store=store)
    benchmark = reg.metrics(mid, "ollama").benchmark
    assert benchmark["bug_fix"].samples == 1
    assert benchmark["repair"].samples == 1
    store.close()


def test_benchmark_vs_production_remain_isolated() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    store.save_benchmark(bench_record(mid))
    store.save_production(
        ProductionSample(
            model_id=mid, engine="ollama", task_class="bug_fix", success=False, id=unique_id("prod")
        )
    )
    reg = ModelRegistry(EventLogger(), metric_store=store)
    metrics = reg.metrics(mid, "ollama")
    assert metrics.benchmark["bug_fix"].samples == 1
    assert metrics.benchmark["bug_fix"].success_rate == 1.0
    assert metrics.production["bug_fix"].samples == 1
    assert metrics.production["bug_fix"].success_rate == 0.0
    # bundle_for keeps production-preferred semantics, never mixed
    assert reg.bundle_for(mid, "ollama", "bug_fix").success_rate == 0.0
    store.close()


def test_engine_identity_preserved_per_record() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    store.save_benchmark(bench_record(mid, engine="ollama"))
    store.save_benchmark(bench_record(mid, engine="llamacpp"))
    engines = sorted(r.engine_id for r in store.load_benchmarks(mid))
    assert engines == ["llamacpp", "ollama"]  # identity never collapsed
    # registry reload: engine-scoped bundles remain separate (Phase 14A.1)
    reg = ModelRegistry(EventLogger(), metric_store=store)
    assert reg.metrics(mid, "ollama").benchmark["bug_fix"].samples == 1
    assert reg.metrics(mid, "llamacpp").benchmark["bug_fix"].samples == 1
    store.close()


def test_benchmark_version_preserved_per_record() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    store.save_benchmark(bench_record(mid))
    v2 = bench_record(mid)
    v2.benchmark_version = "bench-v2"
    store.save_benchmark(v2)
    versions = sorted(r.benchmark_version for r in store.load_benchmarks(mid))
    assert versions == ["bench-v1", "bench-v2"]  # identity never collapsed
    store.close()


# ------------------------------------------------- restart / durability ----


def test_restart_preserves_metrics() -> None:
    mid = unique_id("m")
    store_a = PostgresModelMetricStore(TEST_DSN)
    reg_a = ModelRegistry(EventLogger(), metric_store=store_a)
    reg_a.record_benchmark(bench_record(mid))
    reg_a.record_production(
        ProductionSample(
            model_id=mid, engine="ollama", task_class="coding", success=True, id=unique_id("prod")
        )
    )
    store_a.close()

    # fresh process simulation: new store + new registry over the same database
    store_b = PostgresModelMetricStore(TEST_DSN)
    reg_b = ModelRegistry(EventLogger(), metric_store=store_b)  # reload in construction
    assert reg_b.metrics(mid, "ollama").benchmark["bug_fix"].samples == 1
    assert reg_b.bundle_for(mid, "ollama", "coding").samples == 1
    assert reg_b.metrics(mid, "ollama").production["coding"].samples == 1
    store_b.close()


def test_deterministic_aggregation_after_reload() -> None:
    mid = unique_id("m")
    originals = [bench_record(mid), bench_record(mid)]
    store = PostgresModelMetricStore(TEST_DSN)
    for rec in originals:
        store.save_benchmark(rec)
    loaded_a = store.load_benchmarks(mid)
    loaded_b = store.load_benchmarks(mid)
    assert aggregate_benchmarks(loaded_a) == aggregate_benchmarks(originals)
    assert aggregate_benchmarks(loaded_a) == aggregate_benchmarks(loaded_b)
    # registry reload twice -> identical bundles
    reg_a = ModelRegistry(EventLogger(), metric_store=store)
    reg_b = ModelRegistry(EventLogger(), metric_store=store)
    assert reg_a.metrics(mid, "ollama") == reg_b.metrics(mid, "ollama")
    store.close()


def test_no_records_means_no_invented_metrics() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    reg = ModelRegistry(EventLogger(), metric_store=store)
    bundle = reg.bundle_for(unique_id("m"), "ollama", "coding")
    assert bundle.samples == 0
    assert bundle.success_rate is None
    assert bundle.goal_success_rate is None
    store.close()


# ---------------------------------------------------- fail-closed / dupes --


def test_corrupt_record_fails_closed() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    rec = bench_record(mid)
    store.save_benchmark(rec)
    store._conn.execute(
        "UPDATE nomadic_model_metrics SET record = %s WHERE metric_id = %s",
        ('{"kind": "model_metric", "schema_version": 1, "record": {"broken": true}}', rec.id),
    )
    with pytest.raises(PersistenceCorrupt):
        store.load_benchmarks(mid)
    # test hygiene: remove the injected corruption so shared-DB tests stay isolated
    store._conn.execute("DELETE FROM nomadic_model_metrics WHERE metric_id = %s", (rec.id,))
    store.close()


def test_schema_version_mismatch_fails_closed() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    rec = bench_record(mid)
    store.save_benchmark(rec)
    # corrupt the versioned ENVELOPE (what the loader actually reads)
    store._conn.execute(
        "UPDATE nomadic_model_metrics SET record = jsonb_set(record, '{schema_version}', '99') "
        "WHERE metric_id = %s",
        (rec.id,),
    )
    with pytest.raises(PersistenceCorrupt, match="version"):
        store.load_benchmarks(mid)
    # test hygiene: remove the injected corruption
    store._conn.execute("DELETE FROM nomadic_model_metrics WHERE metric_id = %s", (rec.id,))
    store.close()


def test_source_mismatch_fails_closed() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    rec = bench_record(mid)
    store.save_benchmark(rec)
    # a benchmark row relabelled as production is incompatible evidence
    store._conn.execute(
        "UPDATE nomadic_model_metrics SET source = 'production' WHERE metric_id = %s",
        (rec.id,),
    )
    with pytest.raises(PersistenceCorrupt, match="source"):
        store.load_production(mid)
    # test hygiene: remove the injected corruption
    store._conn.execute("DELETE FROM nomadic_model_metrics WHERE metric_id = %s", (rec.id,))
    store.close()


def test_duplicate_writes_are_idempotent() -> None:
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    rec = bench_record(mid)
    store.save_benchmark(rec)
    store.save_benchmark(rec)  # same metric_id: history is never rewritten
    assert store.load_benchmarks(mid) == [rec]
    store.close()


def test_unavailable_postgresql_is_structured() -> None:
    with pytest.raises(PersistenceUnavailable):
        PostgresModelMetricStore(
            "postgresql://nomadicos:nomadicos@localhost:5999/nomadicos", connect_timeout_s=2.0
        )


# ------------------------------------------------------------- authority ---


def test_metric_persistence_leaves_authority_state_unchanged(tmp_path) -> None:
    from nomadicos.authority.store import AuthorityStore

    authority = AuthorityStore(tmp_path / "authority.json")
    authority.grant_full_pc_autonomy()
    before = authority.state()
    store = PostgresModelMetricStore(TEST_DSN)
    mid = unique_id("m")
    store.save_benchmark(bench_record(mid))
    store.save_production(
        ProductionSample(model_id=mid, task_class="coding", success=True, id=unique_id("prod"))
    )
    after = authority.state()
    assert after.epoch == before.epoch
    assert after.grant == before.grant
    assert after.conflicts == before.conflicts
    assert after.instructions == before.instructions
    store.close()
