"""Focused regression coverage: UNKNOWN engine health is never routable.

The bug: ModelSelector.rank() excluded only ModelHealth.UNHEALTHY, so
ModelHealth.UNKNOWN was routable — for models_dir entries UNKNOWN means the
model file exists but the engine has never proven it is serving the model.
Observed live: Ollama HEALTHY with 8 models, llama.cpp UNHEALTHY (llama-server
not running), models/*.gguf registered with engine=llamacpp and UNKNOWN —
recovery/escalation escalated into an UNKNOWN llamacpp GGUF and generation
failed with EngineUnavailable. Required semantics: HEALTHY and DEGRADED are
routable; UNKNOWN and UNHEALTHY are not; a GGUF file on disk is not proof
of llama.cpp availability; healthy live models with zero measurements keep
cold-start eligibility.
"""

from __future__ import annotations

import pytest

from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.router import ModelSelector
from nomadicos.router.escalation import EscalationPolicy


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def model(mid: str, health: ModelHealth, engine: str = "ollama") -> ModelRecord:
    return ModelRecord(
        model_id=mid,
        engine=engine,  # type: ignore[arg-type]
        roles=["coding"],
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        health=health,
    )


def ranked_ids(reg: ModelRegistry) -> list[str]:
    return [c.model_id for c in ModelSelector(RouterConfig()).rank(reg, reqs())]


# --------------------------------------------------------- routability -----


def test_unknown_model_is_excluded() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(model("unknown_gguf", ModelHealth.UNKNOWN, engine="llamacpp"))
    # the model FILE exists but no engine has proven it serves the model:
    # never routable
    assert ranked_ids(reg) == []


def test_unhealthy_model_is_excluded() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(model("down_model", ModelHealth.UNHEALTHY))
    assert ranked_ids(reg) == []


def test_degraded_model_remains_eligible() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(model("degraded_model", ModelHealth.DEGRADED))
    # existing Phase 14C semantics: DEGRADED stays routable
    assert ranked_ids(reg) == ["degraded_model"]


def test_healthy_unmeasured_model_keeps_cold_start() -> None:
    reg = ModelRegistry(EventLogger())
    reg.register(model("live_unmeasured", ModelHealth.HEALTHY))
    # zero performance measurements: cold start applies deterministically
    ranked_list = ModelSelector(RouterConfig()).rank(reg, reqs())
    assert [c.model_id for c in ranked_list] == ["live_unmeasured"]
    assert ranked_list[0].measured is False


def test_routing_cannot_escalate_into_unknown_llamacpp() -> None:
    # the EXACT reported scenario: Ollama healthy with measured history,
    # llama.cpp unavailable, models/*.gguf registered UNKNOWN via llamacpp
    reg = ModelRegistry(EventLogger())
    reg.register(model("gemma3:4b", ModelHealth.HEALTHY, engine="ollama"))
    reg.register(model("models/gemma-3-4b.gguf", ModelHealth.UNKNOWN, engine="llamacpp"))
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="gemma3:4b", engine="ollama", task_class="coding", success=True
            )
        )
    # initial selection: the healthy Ollama model wins
    selected = ModelSelector(RouterConfig()).select(reg, reqs()).selected
    assert (selected.model_id, selected.engine) == ("gemma3:4b", "ollama")
    # recovery/escalation: with the Ollama model tried, the UNKNOWN llamacpp
    # record is NOT routable — escalation fails closed instead of escalating
    # into an unproven engine
    policy = EscalationPolicy(ModelSelector(RouterConfig()), max_escalations=5)
    with pytest.raises(Exception, match="no untried"):
        policy.next_model(reg, reqs(), tried=["gemma3:4b"], escalation_count=1)


def test_synced_engine_health_makes_models_routable() -> None:
    # the honest path to routability for models_dir entries: engine-proven
    # health (sync_models → sync_from_engine), never the file on disk
    from nomadicos.inference.mock import MockEngine
    from nomadicos.kernel.config import ModelRoles
    from nomadicos.registry.scanner import RegistryBuilder

    reg = ModelRegistry(EventLogger())
    engine = MockEngine()
    engine.register_model("ornith")
    builder = RegistryBuilder(ModelRoles(coding_worker="ornith"))
    import asyncio

    asyncio.run(builder.sync_from_engine(reg, engine))
    assert reg.get("ornith").health is ModelHealth.HEALTHY
    assert "ornith" in ranked_ids(reg)
