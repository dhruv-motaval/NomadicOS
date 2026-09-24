"""Phase 14B deterministic benchmark tests: suite loading, stable ids/
versions, workspace isolation, production-path wiring through the SAME
trusted lifecycle, BenchmarkRecord identity, MetricStore persistence,
honest negative behavior, authority isolation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bench_helpers import make_app, write_json

from nomadicos.evaluation.benchmarking import TaskRunOutcome
from nomadicos.evaluation.suite import (
    BENCHMARK_VERSION,
    SUITE,
    expected_for,
    get_task,
    suite_ids,
)
from nomadicos.orchestration.app import NomadicApp

# ------------------------------------------------------------ suite ----


def test_suite_loading() -> None:
    assert len(SUITE) == 6
    for benchmark_id in suite_ids():
        task = get_task(benchmark_id)
        assert task is not None
        assert task.objective
    assert get_task("bench_nonexistent_999") is None


def test_stable_benchmark_ids() -> None:
    import re

    ids = suite_ids()
    assert ids == [task.id for task in SUITE]
    assert ids == suite_ids()  # deterministic across calls
    for benchmark_id in ids:
        # never UUIDs/timestamps/temp paths: declared stable ids only
        assert re.fullmatch(r"bench_[a-z_]+_\d{3}", benchmark_id), benchmark_id


def test_stable_benchmark_version() -> None:
    assert BENCHMARK_VERSION == "1"
    assert expected_for("bench_fs_create_001") == {
        "expected_success": True,
        "expectation": "goal_pass",
    }
    assert expected_for("bench_neg_unverifiable_001") == {
        "expected_success": False,
        "expectation": "goal_not_verified",
    }


# -------------------------------------------------- positive execution --


async def test_mock_benchmark_execution_passes(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # SUCCESS only through the GoalVerifier: predicate file_content_equals
    assert record.success is True
    assert record.goal_verified is True
    assert record.failure_type is None
    assert record.steps >= 1


async def test_production_path_wiring(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # the record was produced through the existing run_workspace_task seam:
    # registry.record_benchmark → engine-scoped MetricStore (Phase 14A)
    bundle = app.registry.metrics("models/small.gguf", "mock").benchmark
    assert bundle["feature_implementation"].samples == 1
    assert record.task_category == "feature_implementation"


async def test_benchmark_record_identity_preserved(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # identity: model_id / engine / benchmark_id / benchmark_version preserved
    assert record.model_id == "models/small.gguf"
    assert record.engine_id == "mock"
    assert record.task_id == "bench_fs_create_001"
    assert record.benchmark_version == BENCHMARK_VERSION
    assert record.id.startswith("bench")
    assert record.total_latency_ms is not None and record.total_latency_ms >= 0
    assert record.environment["platform"]
    assert record.language == "python"


async def test_terminal_benchmark_exit_code(benchmark_app: NomadicApp) -> None:
    import sys

    app = benchmark_app
    app.mock.script(
        "terminal probe",
        json_terminal(sys.executable, "-c", "print('terminal probe ok')"),
    )
    task = get_task("bench_term_exit_001")
    record = await app.run_benchmark("models/small.gguf", task)
    assert record.success is True
    assert record.goal_verified is True


def json_terminal(command: str, *args: str) -> str:
    import json

    return json.dumps(
        {
            "tool": "terminal",
            "operation": "execute",
            "args": {"command": command, "args": list(args)},
        }
    )


# ------------------------------------------------------------ isolation --


async def test_workspace_isolation(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    user_ws = app.runtime.workspace_root
    before = sorted(str(p) for p in user_ws.rglob("*"))
    temp_before = set(tempfile.gettempdir() and Path(tempfile.gettempdir()).glob("nomadic-bench-*"))
    task = get_task("bench_fs_create_001")
    await app.run_benchmark("models/small.gguf", task)
    # the user's real workspace was never touched
    after = sorted(str(p) for p in user_ws.rglob("*"))
    assert after == before
    # deterministic cleanup: no benchmark workspace leaked
    temp_after = set(Path(tempfile.gettempdir()).glob("nomadic-bench-*"))
    assert temp_after == temp_before


async def test_benchmark_cannot_mutate_authority(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    before = app.store.state()
    task = get_task("bench_fs_create_001")
    await app.run_benchmark("models/small.gguf", task)
    after = app.store.state()
    assert after.epoch == before.epoch
    assert after.grant == before.grant


async def test_normal_persistent_memory_untouched(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    memory_file = Path(str(app.runtime.config.persistence.state_dir)) / "memory.jsonl"
    before = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    task = get_task("bench_fs_create_001")
    await app.run_benchmark("models/small.gguf", task)
    after = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    # benchmark-owned memory only: normal persistent memory never modified
    assert after == before


# ------------------------------------------------------------ negatives --


async def test_negative_benchmark_recorded_honestly(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("summary", write_json("summary.txt", "repair done"))
    task = get_task("bench_neg_unverifiable_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # expected failure: tests_pass never runs ⇒ GoalVerifier NOT_VERIFIED;
    # never manufactured into SUCCESS
    assert record.success is False
    assert record.goal_verified is False
    assert record.failure_type in ("GOAL_NOT_VERIFIED", "NO_ACTION_AUTHORIZED")


async def test_invalid_model_output_recorded_honestly(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", "not valid action json {{{")
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    assert record.success is False
    assert record.goal_verified is False
    assert record.failure_type


async def test_unauthorized_benchmark_is_blocked(tmp_path: Path) -> None:
    from bench_helpers import make_app

    app = make_app(tmp_path, grant=False)
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # authorization enforced: no execution happened, recorded honestly
    assert record.success is False
    assert record.goal_verified is False
    assert record.failure_type in ("OWNER_CONFLICT", "NO_ACTION_AUTHORIZED")
    assert record.tool_success in (None, False)


async def test_unavailable_capability_is_recorded_honestly(tmp_path: Path) -> None:
    from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord

    app = make_app(tmp_path)
    # only the INCAPABLE model is registered: the benchmark task requires
    # TOOL_USE (+ REASONING via the task analyzer) which it does NOT have,
    # so routing must refuse it - the capability requirement is not bypassed
    app.registry.remove("models/small.gguf")
    app.registry.register(
        ModelRecord(
            model_id="models/nocap.gguf",
            engine="mock",
            roles=["worker"],
            capabilities=[CapabilityTag.TEXT],
            health=ModelHealth.HEALTHY,
            source="manual",
            context_window=65536,
        )
    )
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    before = app.store.state()
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/nocap.gguf", task)
    # no routable model: recorded honestly, no fabricated SUCCESS
    assert record.success is False
    assert record.goal_verified is False
    assert record.failure_type == "NO_ACTION_AUTHORIZED"
    # routing/execution never bypassed the capability requirement
    assert record.steps == 0
    assert record.tool_success is None
    assert record.model_id == "models/nocap.gguf"
    # authority state untouched
    after = app.store.state()
    assert after.epoch == before.epoch
    assert after.grant == before.grant


# ------------------------------------------------- MetricStore / router --


async def test_metric_store_persistence_through_benchmark(benchmark_app: NomadicApp) -> None:
    from bench_helpers import FakeMetricStore

    from nomadicos.registry.model_registry import ModelRegistry

    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    store = FakeMetricStore()
    app.registry.wire_metric_store(store)
    task = get_task("bench_fs_create_001")
    record = await app.run_benchmark("models/small.gguf", task)
    # the benchmark record persisted through the Phase 14A MetricStore seam
    assert store.benchmarks == [record]
    # reload: a fresh registry rehydrates the same engine-scoped evidence
    fresh = ModelRegistry(app.registry.logger)
    fresh.wire_metric_store(store)
    bundle = fresh.metrics("models/small.gguf", "mock").benchmark
    assert bundle["feature_implementation"].samples == 1


async def test_benchmark_does_not_touch_production_history(benchmark_app: NomadicApp) -> None:
    app = benchmark_app
    app.mock.script("benchmark_output", write_json("benchmark_output.txt", "NOMADICOS_14B_PASS"))
    task = get_task("bench_fs_create_001")
    await app.run_benchmark("models/small.gguf", task)
    # production routing history (Phase 14A.2) is never fed by benchmarks
    assert app.registry.metrics("models/small.gguf", "mock").production == {}


def test_outcome_semantics_types() -> None:
    outcome = TaskRunOutcome(success=False, goal_verified=False, failure_type="GOAL_NOT_VERIFIED")
    assert outcome.success is False
    assert outcome.goal_verified is False
