"""Measured capability evidence, not role labels, drives specialist routing."""

from __future__ import annotations

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.selection import ModelSelector


def _model(model_id: str, roles: list[str], caps: list[CapabilityTag]) -> ModelRecord:
    return ModelRecord(
        model_id=model_id,
        engine="mock",
        roles=roles,  # type: ignore[arg-type]
        capabilities=caps,
        health=ModelHealth.HEALTHY,
    )


def _coding_requirements() -> TaskRequirements:
    return TaskRequirements(
        task_type=TaskType.CODING,
        capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        difficulty=0.5,
    )


def test_measured_coding_capability_beats_declared_role() -> None:
    registry = ModelRegistry(EventLogger())
    registry.register(
        _model(
            "declared-coder",
            ["coding"],
            [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING],
        )
    )
    registry.register(
        _model("measured-general", ["worker"], [CapabilityTag.TEXT, CapabilityTag.TOOL_USE])
    )
    for index in range(3):
        registry.record_benchmark(
            BenchmarkRecord(
                id=f"bench-{index}",
                model_id="measured-general",
                engine_id="mock",
                benchmark_version="cognitive-v1",
                task_id=f"coding-{index}",
                task_category="feature_implementation",
                capability=CapabilityTag.CODING.value,
                language="python",
                difficulty=0.5,
                success=True,
                environment={},
            )
        )

    result = ModelSelector(RouterConfig()).select(registry, _coding_requirements())
    assert result.selected.model_id == "measured-general"
    assert result.ranked[0].measured is True


def test_capability_bundle_excludes_unattributed_legacy_records() -> None:
    registry = ModelRegistry(EventLogger())
    registry.register(_model("m", ["coding"], [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]))
    registry.record_benchmark(
        BenchmarkRecord(
            id="legacy",
            model_id="m",
            engine_id="mock",
            benchmark_version="v1",
            task_id="t",
            task_category="feature_implementation",
            language="python",
            difficulty=0.5,
            success=True,
            environment={},
        )
    )
    assert registry.capability_bundle("m", "mock", CapabilityTag.CODING.value).samples == 0
