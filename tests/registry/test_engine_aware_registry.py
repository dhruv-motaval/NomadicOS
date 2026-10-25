"""Focused regression coverage (Phase 14C-F): engine-aware registry identity.

The Phase 14C routing audit CONFIRMED that ModelRegistry was keyed only by
model_id, so the same model_id under two engines could not coexist. 14C-F
keys records by (model_id, engine): both engines coexist, registering one
never overwrites the other, bare lookups stay deterministic (single record
returns it; several raise fail-closed), and routing uses the exact engine
record. Metric evidence stays engine-scoped (14A) and version-scoped
(14C-A).
"""

from __future__ import annotations

import pytest

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.registry.scanner import RegistryBuilder
from nomadicos.router import ModelSelector


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def record(mid: str, engine: str, **over) -> ModelRecord:
    return ModelRecord(
        model_id=mid,
        engine=engine,  # type: ignore[arg-type]
        roles=["coding"],
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        health=ModelHealth.HEALTHY,
        **over,
    )


def bench(mid: str, engine: str, success: bool, version: str = "bench-v1") -> BenchmarkRecord:
    return BenchmarkRecord(
        id=f"bench_{engine}_{int(success)}_{version}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version=version,
        task_id="t",
        task_category="bug_fix",  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=success,
        total_latency_ms=100.0 if success else 500.0,
    )


# ----------------------------------------------------- engine identity -----


def test_same_model_id_on_two_engines_coexist() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "ollama"))
    reg.register(record("X", "llamacpp"))
    # both engines coexist; neither overwrites the other
    assert len(reg.all()) == 2
    assert reg.get("X", "ollama").engine == "ollama"
    assert reg.get("X", "llamacpp").engine == "llamacpp"
    assert reg.get("X", "ollama") is not reg.get("X", "llamacpp")


def test_exact_lookup_and_bare_lookup_semantics() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("single", "ollama"))
    # single-engine registration: bare lookup resolves deterministically
    assert reg.get("single").engine == "ollama"
    reg.register(record("dual", "ollama"))
    reg.register(record("dual", "llamacpp"))
    # several records for one model_id: bare lookup fails CLOSED
    with pytest.raises(ResourceUnavailable, match="multiple engines"):
        reg.get("dual")
    # exact engine identity resolves
    assert reg.get("dual", "llamacpp").engine == "llamacpp"
    with pytest.raises(ResourceUnavailable, match="not registered on engine"):
        reg.get("dual", "mock")


def test_enabled_listing_is_deterministic_and_engine_aware() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "llamacpp"))
    reg.register(record("X", "ollama"))
    reg.register(record("Y", "ollama"))
    ids = [(r.model_id, r.engine) for r in reg.enabled()]
    # deterministic order, both engine records listed
    assert ids == [("X", "llamacpp"), ("X", "ollama"), ("Y", "ollama")]
    # disable one engine record; the other stays enabled
    reg.get("X", "llamacpp").enabled = False
    x_enabled = [(r.model_id, r.engine) for r in reg.enabled() if r.model_id == "X"]
    assert x_enabled == [("X", "ollama")]


def test_remove_is_engine_aware() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "ollama"))
    reg.register(record("X", "llamacpp"))
    reg.remove("X", "ollama")  # exact engine removal
    assert reg.get("X", "llamacpp") is not None
    reg.remove("X")  # bare model_id: deterministic sweep of all engines
    assert len(reg.all()) == 0


# ------------------------------------------------- routing isolation -------


def test_routing_uses_exact_engine_record_and_isolation() -> None:
    reg = ModelRegistry(EventLogger())
    # X on both engines: weak llamacpp production history, strong ollama
    reg.register(record("X", "llamacpp"))
    reg.register(record("X", "ollama"))
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="X", engine="llamacpp", task_class="coding", success=False)
        )
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True)
        )
    sel = ModelSelector(RouterConfig())
    ranked = sel.rank(reg, reqs())
    # metric isolation: each engine's evidence is its own (no blended 0.5)
    assert reg.bundle_for("X", "ollama", "coding").success_rate == 1.0
    assert reg.bundle_for("X", "llamacpp", "coding").success_rate == 0.0
    # the router selects through the exact engine identity; the weak
    # llamacpp evidence fails the gate so the ollama record wins
    selected = sel.select(reg, reqs()).selected
    assert (selected.model_id, selected.engine) == ("X", "ollama")
    engines = {c.engine for c in ranked if c.model_id == "X"}
    assert engines == {"ollama"}


def test_version_isolation_with_engine_aware_registry() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "ollama"))
    reg.register(record("X", "llamacpp"))
    reg.record_benchmark(bench("X", "ollama", True, version="bench-v1"))
    reg.record_benchmark(bench("X", "ollama", False, version="bench-v2"))
    reg.record_benchmark(bench("X", "llamacpp", True, version="bench-v2"))
    # per-engine, per-version bundles never blend
    assert (
        reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v1").success_rate == 1.0
    )
    assert (
        reg.bundle_for("X", "ollama", "bug_fix", benchmark_version="bench-v2").success_rate == 0.0
    )
    assert (
        reg.bundle_for("X", "llamacpp", "bug_fix", benchmark_version="bench-v2").success_rate == 1.0
    )
    assert reg.metrics_versions("X", "ollama") == ["bench-v1", "bench-v2"]


def test_escalation_preserves_engine_identity() -> None:
    from nomadicos.router.escalation import EscalationPolicy

    reg = ModelRegistry(EventLogger())
    # dual-registered model + a second model: escalation walks the ranked
    # chain with exact engine identity (never an arbitrary engine pick)
    reg.register(record("X", "ollama"))
    reg.register(record("Y", "llamacpp"))
    policy = EscalationPolicy(ModelSelector(RouterConfig()), max_escalations=5)
    first = policy.next_model(reg, reqs(), tried=[], escalation_count=0)
    assert (first.model_id, first.engine) == ("X", "ollama")
    second = policy.next_model(reg, reqs(), tried=["X"], escalation_count=1)
    assert (second.model_id, second.engine) == ("Y", "llamacpp")
    with pytest.raises(Exception, match="no untried"):
        policy.next_model(reg, reqs(), tried=["X", "Y"], escalation_count=2)


def test_production_samples_engine_scoped_with_dual_registration() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "ollama"))
    reg.register(record("X", "llamacpp"))
    reg.record_production(
        ProductionSample(model_id="X", engine="ollama", task_class="coding", success=True)
    )
    reg.record_production(
        ProductionSample(model_id="X", engine="llamacpp", task_class="coding", success=False)
    )
    assert reg.bundle_for("X", "ollama", "coding").success_rate == 1.0
    assert reg.bundle_for("X", "llamacpp", "coding").success_rate == 0.0


async def test_model_sync_and_backward_compatibility(tmp_path) -> None:
    from nomadicos.inference.mock import MockEngine
    from nomadicos.kernel.config import ModelRoles

    reg = ModelRegistry(EventLogger())
    builder = RegistryBuilder(ModelRoles())
    engine = MockEngine()
    engine.register_model("ornith")
    # engine sync registers the engine-reported identity
    await builder.sync_from_engine(reg, engine)
    assert reg.get("ornith").engine == "mock"  # single-engine: bare lookup works
    # models/ dir registration (different id namespace, llamacpp engine)
    (tmp_path / "m.gguf").write_text("fake", encoding="utf-8")
    builder.build(reg, models_dir=tmp_path)
    assert (reg.get("models/m.gguf", "llamacpp").model_id) == "models/m.gguf"
    # backward compatibility: a bare register/get round trip still works
    reg.register(record("plain", "mock"))
    assert reg.get("plain").model_id == "plain"


def test_health_is_engine_scoped() -> None:
    from nomadicos.contracts.model import ModelHealth

    reg = ModelRegistry(EventLogger())
    reg.register(record("X", "ollama"))
    reg.register(record("X", "llamacpp"))
    reg.set_health("X", ModelHealth.UNHEALTHY, "llamacpp")  # exact engine health
    assert reg.get("X", "llamacpp").health is ModelHealth.UNHEALTHY
    # the other engine record keeps its own declared health (never shared)
    assert reg.get("X", "ollama").health is ModelHealth.HEALTHY
    # ambiguous bare health set fails closed
    with pytest.raises(ResourceUnavailable, match="multiple engines"):
        reg.set_health("X", ModelHealth.HEALTHY)
