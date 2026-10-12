"""Phase 14B: PostgreSQL MetricStore persistence of benchmark records
produced by the real production-path run (integration-marked; requires the
docker-compose.dev.yml PostgreSQL; never faked with the file adapter)."""

from __future__ import annotations

import os
import uuid

import pytest
from bench_helpers import make_app, write_json

from nomadicos.evaluation.suite import get_task
from nomadicos.persistence.errors import PersistenceUnavailable
from nomadicos.registry.model_registry import ModelRegistry

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


def unique_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


async def test_benchmark_persists_through_postgres_metric_store(tmp_path) -> None:
    from nomadicos.persistence.metrics import PostgresModelMetricStore

    model_id = f"models/bench_{unique_id('pg')}.gguf"
    app = make_app(tmp_path)
    from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord

    app.registry.register(
        ModelRecord(
            model_id=model_id,
            engine="mock",
            roles=["worker", "coding"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.HEALTHY,
            source="manual",
            context_window=65536,
        )
    )
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    try:
        store = PostgresModelMetricStore(TEST_DSN)
    except PersistenceUnavailable as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc.message[:100]}")
    app.registry.wire_metric_store(store)
    task = get_task("bench_fs_create_001")
    try:
        record = await app.run_benchmark(model_id, task)
        assert record is not None and record.success is True
        # PostgreSQL reload: a fresh registry rehydrates the SAME record
        fresh = ModelRegistry(app.registry.logger)
        fresh.wire_metric_store(PostgresModelMetricStore(TEST_DSN))
        bundle = fresh.metrics(model_id, "mock").benchmark
        assert bundle["feature_implementation"].samples == 1
        loaded = store.load_benchmarks(model_id)
        assert len(loaded) == 1
        assert loaded[0].model_id == model_id
        assert loaded[0].engine_id == "mock"
        assert loaded[0].benchmark_version == record.benchmark_version
        assert loaded[0].task_id == "bench_fs_create_001"
    finally:
        store.close()
