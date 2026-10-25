"""Focused regression coverage (Phase 14C-H): measurement-aware critic
selection.

select_critic() previously used size only and ignored measurements. 14C-H
prefers critics with SUFFICIENT measured evidence (samples >=
minimum_sample_count, engine-exact) ordered by measured quality, then size;
without measurements the existing size-based fallback applies
deterministically. Critic metrics never influence the worker selection path.
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
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, ProductionSample
from nomadicos.router import ModelSelector


def reqs() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.6,
    )


def critic(
    mid: str,
    *,
    size: float = 7.0,
    health: ModelHealth = ModelHealth.HEALTHY,
    engine: str = "ollama",
    roles: list[str] | None = None,
) -> ModelRecord:
    return ModelRecord(
        model_id=mid,
        engine=engine,  # type: ignore[arg-type]
        roles=roles or ["critic"],
        capabilities=[CapabilityTag.TEXT, CapabilityTag.REASONING],
        health=health,
        params={"size_hint_b": size},
    )


def bench_record(mid: str, engine: str, success: bool, category: str = "bug_fix"):
    from nomadicos.contracts.benchmark import BenchmarkRecord

    return BenchmarkRecord(
        id=f"bench_{mid}_{category}_{int(success)}",
        model_id=mid,
        engine_id=engine,  # type: ignore[arg-type]
        benchmark_version="bench-v1",
        task_id="t",
        task_category=category,  # type: ignore[arg-type]
        language="python",
        difficulty=0.3,
        success=success,
    )


def critic_registry(records: list[ModelRecord]) -> ModelRegistry:
    reg = ModelRegistry(EventLogger())
    for r in records:
        reg.register(r)
    return reg


# ------------------------------------------------------- critic selection --


def test_measured_critic_evidence_preferred() -> None:
    # big-but-unmeasured vs smaller-but-measured-strong: measured wins
    reg = critic_registry([critic("big_unmeasured", size=30.0), critic("small_measured", size=7.0)])
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="small_measured", engine="ollama", task_class="review", success=True
            )
        )
    selected = ModelSelector(RouterConfig()).select_critic(reg)
    assert selected.model_id == "small_measured"
    assert (selected.model_id, selected.engine) == ("small_measured", "ollama")


def test_measured_quality_orders_critics() -> None:
    reg = critic_registry(
        [
            critic("weak_measured", size=7.0),
            critic("strong_measured", size=7.0),
        ]
    )
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="weak_measured", engine="ollama", task_class="review", success=False
            )
        )
    for _ in range(3):
        reg.record_production(
            ProductionSample(
                model_id="strong_measured", engine="ollama", task_class="review", success=True
            )
        )
    assert ModelSelector(RouterConfig()).select_critic(reg).model_id == "strong_measured"


def test_insufficient_evidence_falls_back_to_size() -> None:
    # one sample below the floor is insufficient: existing size-based
    # fallback applies deterministically
    reg = critic_registry(
        [critic("small_thin", size=7.0), critic("big_thin", size=30.0)],
    )
    reg.record_production(
        ProductionSample(model_id="small_thin", engine="ollama", task_class="review", success=True)
    )
    selected = ModelSelector(RouterConfig()).select_critic(reg)
    assert selected.model_id == "big_thin"  # strongest plausible first (size)


def test_no_measurements_at_all_uses_size_fallback() -> None:
    reg = critic_registry([critic("small", size=7.0), critic("big", size=30.0)])
    assert ModelSelector(RouterConfig()).select_critic(reg).model_id == "big"
    # deterministic across calls
    assert ModelSelector(RouterConfig()).select_critic(reg).model_id == "big"


def test_unhealthy_critic_excluded() -> None:
    reg = critic_registry(
        [
            critic("sick_big", size=30.0, health=ModelHealth.UNHEALTHY),
            critic("healthy_small", size=7.0),
        ]
    )
    assert ModelSelector(RouterConfig()).select_critic(reg).model_id == "healthy_small"


def test_engine_isolation_for_critics() -> None:
    # the same critic registered on two engines: evidence is engine-exact
    reg = critic_registry(
        [
            critic("critic", size=7.0, engine="ollama"),
            critic("critic", size=7.0, engine="llamacpp"),
        ]
    )
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="critic", engine="ollama", task_class="review", success=True)
        )
    # llamacpp engine has no measured evidence -> bare lookup is ambiguous,
    # but select_critic resolves the exact engine record
    selected = ModelSelector(RouterConfig()).select_critic(reg)
    assert selected.engine == "ollama"


def test_critic_metrics_do_not_influence_worker_path() -> None:
    reg = critic_registry([critic("critic", size=7.0)])
    for _ in range(3):
        reg.record_production(
            ProductionSample(model_id="critic", engine="ollama", task_class="review", success=True)
        )
    # the worker selection path for a CODING task never sees the critic's
    # review evidence (different task_class and role requirements)
    with pytest.raises(ResourceUnavailable):
        ModelSelector(RouterConfig()).select(reg, reqs())
