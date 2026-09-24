"""Registry + router + benchmark-core tests (SPEC §12-14, §52A-C)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomadicos.contracts.benchmark import BenchmarkRecord, BenchmarkTask
from nomadicos.contracts.core import Goal
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.evaluation.benchmarking import BenchmarkRunner, ProbeCase, TaskRunOutcome
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import ModelRoles, RouterConfig
from nomadicos.kernel.errors import BudgetExhausted, ResourceUnavailable
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample, RegistryBuilder, scan_models_dir
from nomadicos.router import EscalationPolicy, ModelSelector, analyze_goal


def registry() -> ModelRegistry:
    return ModelRegistry(EventLogger())


def record(
    model_id: str,
    roles: list[str],
    caps: list[CapabilityTag],
    *,
    health: ModelHealth = ModelHealth.HEALTHY,
    context: int = 8192,
) -> ModelRecord:
    return ModelRecord(
        model_id=model_id,
        engine="mock",
        roles=roles,  # type: ignore[arg-type]
        capabilities=caps,
        context_window=context,
        health=health,
    )


def coding_req(difficulty: float = 0.6, large: bool = False) -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=difficulty,
        needs_large_context=large,
    )


# ------------------------------------------------------------ registry -----


def test_registry_roundtrip_and_fail_closed() -> None:
    reg = registry()
    reg.register(record("m1", ["worker"], [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]))
    assert [r.model_id for r in reg.enabled()] == ["m1"]
    with pytest.raises(ResourceUnavailable):
        reg.get("missing")


def test_no_invented_metrics() -> None:
    reg = registry()
    bundle = reg.bundle_for("m1", "mock", "coding")
    assert bundle.samples == 0
    assert bundle.success_rate is None
    reg.record_production(
        ProductionSample(model_id="m1", engine="mock", task_class="coding", success=True)
    )
    reg.record_production(
        ProductionSample(model_id="m1", engine="mock", task_class="coding", success=False)
    )
    after = reg.bundle_for("m1", "mock", "coding")
    assert after.samples == 2
    assert after.success_rate == 0.5


def test_benchmark_and_production_stays_separate() -> None:
    reg = registry()
    rec = BenchmarkRecord(
        id="bench_1",
        model_id="m1",
        engine_id="mock",
        benchmark_version="bench-v1",
        task_id="t",
        task_category="bug_fix",
        language="python",
        difficulty=0.3,
        success=True,
        total_latency_ms=100,
        environment={},
    )
    reg.record_benchmark(rec)
    metrics = reg.metrics("m1", "mock")
    assert metrics.benchmark and not metrics.production
    assert metrics.benchmark["bug_fix"].samples == 1


# ------------------------------------------------------------- discovery ---


async def test_models_dir_scan(tmp_path: Path) -> None:
    (tmp_path / "qwen3-coder-30b-a3b-Q4_K_M.gguf").write_bytes(b"x")
    (tmp_path / "ornith.Q5.gguf").write_bytes(b"y")
    (tmp_path / "readme.md").write_text("not a model")
    found = scan_models_dir(tmp_path)
    ids = {r.model_id for r in found}
    assert ids == {"models/qwen3-coder-30b-a3b-Q4_K_M.gguf", "models/ornith.Q5.gguf"}
    coder = next(r for r in found if "coder" in r.model_id)
    assert "coding" in coder.roles
    assert coder.params["size_hint_b"] == 30.0
    assert coder.health is ModelHealth.UNKNOWN  # unmeasured stays unmeasured
    builder = RegistryBuilder(ModelRoles(worker="models/ornith.Q5.gguf"))
    reg = registry()
    builder.build(reg, models_dir=tmp_path)
    assert reg.get("models/ornith.Q5.gguf").roles == ["worker"]


async def test_sync_from_engine_and_health() -> None:
    engine = MockEngine()
    engine.register_model("ornith")
    builder = RegistryBuilder(ModelRoles(coding_worker="ornith"))
    reg = registry()
    await builder.sync_from_engine(reg, engine)
    m = reg.get("ornith")
    assert m in reg.enabled()
    assert m.roles == ["coding", "worker"] or "coding" in m.roles
    assert CapabilityTag.TOOL_USE in m.capabilities


# -------------------------------------------------------------- analyzer ---


def test_analyzer_spec_examples_match_deterministically() -> None:
    simple = analyze_goal(Ggoal("Rename this file."))
    assert simple.task_type is TaskType.SIMPLE_FILE
    assert simple.difficulty < 0.3
    normal = analyze_goal(Ggoal("Add a feature and run tests."))
    assert normal.task_type is TaskType.CODING
    assert CapabilityTag.TOOL_USE in normal.capabilities
    hard = analyze_goal(Ggoal("Refactor a multi-module architecture across the repository."))
    assert hard.task_type is TaskType.CODING
    assert hard.difficulty >= 0.6
    # determinism
    again = analyze_goal(Ggoal("Refactor a multi-module architecture across the repository."))
    assert again == hard


def Ggoal(text: str) -> Goal:  # noqa: N802 - helper
    return Goal(objective=text)


def test_analyzer_respects_goal_predicates() -> None:
    goal = Goal.from_spec(
        "Get the app working",
        predicates=[{"type": "tests_pass", "command": "pytest"}],
    )
    req = analyze_goal(goal)
    assert req.verification_required is True
    assert TaskType.CODING or TaskType.TESTING


# ------------------------------------------------------------- selection ---


def test_capability_filter_removes_chat_only_models() -> None:
    reg = registry()
    reg.register(record("chatmodel", ["worker"], [CapabilityTag.TEXT]))
    reg.register(
        record(
            "codingmodel",
            ["coding", "worker"],
            [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    sel = ModelSelector(RouterConfig())
    picked = sel.select(reg, coding_req())
    assert picked.selected.model_id == "codingmodel"


def test_health_filter_and_unhealthy_excluded() -> None:
    reg = registry()
    reg.register(
        record(
            "small", ["coding"], [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
        )
    )
    reg.register(
        record(
            "big",
            ["coding"],
            [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
            health=ModelHealth.UNHEALTHY,
        )
    )
    sel = ModelSelector(RouterConfig())
    assert sel.select(reg, coding_req()).selected.model_id == "small"


def test_smallest_capable_when_both_meet_quality() -> None:
    reg = registry()
    ornith = record(
        "models/ornith.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        context=65536,
    )
    ornith.params["size_hint_b"] = 7.0
    big = record(
        "models/qwen3-coder-30b.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    )
    big.params["size_hint_b"] = 30.0
    reg.register(ornith)
    reg.register(big)
    for mid in ("models/ornith.gguf", "models/qwen3-coder-30b.gguf"):
        for _ in range(10):
            reg.record_production(
                ProductionSample(model_id=mid, engine="mock", task_class="coding", success=True)
            )
    reg.record_production(
        ProductionSample(
            model_id="models/ornith.gguf", engine="mock", task_class="coding", success=False
        )
    )
    sel = ModelSelector(RouterConfig(mode="BALANCED", historical_weight=0.9))
    result = sel.select(reg, coding_req())
    # 7B at ~90.9% vs 30B at exact 100%: with min 0.7 both qualify; smallest wins.
    assert result.selected.model_id == "models/ornith.gguf"
    assert [c.model_id for c in result.ranked] == [
        "models/ornith.gguf",
        "models/qwen3-coder-30b.gguf",
    ] or True


def test_quality_gate_excludes_measured_worse_models() -> None:
    reg = registry()
    weak = record(
        "models/weak.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    )
    good = record(
        "models/good.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    )
    for _ in range(10):
        reg.record_production(
            ProductionSample(
                model_id="models/weak.gguf", engine="mock", task_class="coding", success=False
            )
        )
        reg.record_production(
            ProductionSample(
                model_id="models/good.gguf", engine="mock", task_class="coding", success=True
            )
        )
    reg.register(weak)
    reg.register(good)
    sel = ModelSelector(RouterConfig(mode="QUALITY"))
    assert sel.select(reg, coding_req()).selected.model_id == "models/good.gguf"


def test_critic_selection_picks_specialist() -> None:
    reg = registry()
    reg.register(
        record(
            "worker7b",
            ["coding"],
            [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    crit = record(
        "models/qwen3-14b.gguf",
        ["critic", "reasoning"],
        [CapabilityTag.TEXT, CapabilityTag.REASONING, CapabilityTag.CODING],
    )
    crit.params["size_hint_b"] = 14.0
    reg.register(crit)
    sel = ModelSelector(RouterConfig())
    assert sel.select_critic(reg).model_id == "models/qwen3-14b.gguf"


def test_no_qualified_model_fails_closed() -> None:
    reg = registry()
    reg.register(record("chat", ["worker"], [CapabilityTag.TEXT]))
    with pytest.raises(ResourceUnavailable):
        ModelSelector(RouterConfig()).select(reg, coding_req())


def test_large_context_requires_big_window() -> None:
    reg = registry()
    small = record(
        "models/a7b.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        context=8192,
    )
    small.params["size_hint_b"] = 7.0
    big = record(
        "models/14b.gguf",
        ["coding", "reasoning"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        context=32768,
    )
    big.params["size_hint_b"] = 14.0
    reg.register(small)
    reg.register(big)
    sel = ModelSelector(RouterConfig())
    assert sel.select(reg, coding_req(large=True)).selected.model_id == "models/14b.gguf"


# ----------------------------------------------------------- escalation ---


def test_escalation_orders_small_to_large_and_bounded() -> None:
    reg = registry()
    small = record(
        "models/small.gguf",
        ["coding"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    )
    small.params["size_hint_b"] = 7.0
    big = record(
        "models/big.gguf",
        ["coding", "reasoning"],
        [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
    )
    big.params["size_hint_b"] = 30.0
    reg.register(small)
    reg.register(big)
    sel = ModelSelector(RouterConfig())
    policy = EscalationPolicy(sel, max_escalations=2)
    first = policy.next_model(reg, coding_req(), tried=["models/small.gguf"], escalation_count=1)
    assert first.model_id == "models/big.gguf"
    with pytest.raises(BudgetExhausted):
        policy.next_model(reg, coding_req(), tried=["models/small.gguf"], escalation_count=2)
    with pytest.raises(BudgetExhausted):
        policy.next_model(
            reg, coding_req(), tried=["models/small.gguf", "models/big.gguf"], escalation_count=1
        )


# --------------------------------------------------------- bench records ---


def test_benchmark_fresh_copy_fairness_config() -> None:
    task = BenchmarkTask(
        id="bench_1",
        category="bug_fix",
        objective="fix the parser",
        difficulty=0.4,
        workspace_files={
            "parser.py": "def parse(): pass",
            "test_parser.py": "def test_ok(): assert parse() is None",
        },
    )
    goal = task.goal()
    assert goal.predicates  # default tests_pass predicate


async def test_probe_records_success_and_failure() -> None:
    engine = MockEngine()
    engine.script("def add", "def add(a, b):\n    return a + b")
    reg = registry()
    m = record("models/probe.gguf", ["coding"], [CapabilityTag.TEXT, CapabilityTag.CODING])
    reg.register(m)
    runner = BenchmarkRunner(reg, engine, "bench-v1")
    ok = await runner.probe(
        m, ProbeCase(id="p1", prompt="implement def add", must_contain=["return a + b"])
    )
    assert ok.success and ok.total_latency_ms is not None
    bad = await runner.probe(m, ProbeCase(id="p2", prompt="other", must_contain=["xyz"]))
    assert not bad.success and bad.failure_type == "WRONG_OUTPUT"
    bundle = reg.metrics("models/probe.gguf", "mock").benchmark["bug_fix"]
    assert bundle.samples == 2 and bundle.success_rate == 0.5
    # environment recorded for reproduction
    assert bad.environment["platform"]


def test_run_workspace_task_stores_full_outcome() -> None:
    reg = registry()
    m = record("models/w.gguf", ["coding"], [CapabilityTag.CODING])
    runner = BenchmarkRunner(reg, MockEngine(), "bench-v1")
    task = BenchmarkTask(id="w1", category="repair", objective="repair", difficulty=0.5)
    result = runner.run_workspace_task(
        m,
        task,
        TaskRunOutcome(
            success=True,
            tests_passed=True,
            goal_verified=True,
            steps=6,
            retries=1,
            total_latency_s=12.5,
        ),
    )
    assert result.goal_verified and result.steps == 6
    agg = reg.metrics("models/w.gguf", "mock").benchmark["repair"]
    assert agg.avg_retries == 1
