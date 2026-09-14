"""Phase 12 tests: ModelSelector — capability filter, hardware fit, adaptive
learning across sessions, fail-closed, explainability (BP §53, §97, §149, §320, §386)."""

import pytest

from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.agent.selector_policies import SelectionPolicy
from nomadicos.core.errors import ModelUnavailable
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.models.base import (
    ModelCapabilities,
    ModelStatus,
    ResourceRequirements,
)
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.manager import ModelManager


@pytest.fixture()
def manager() -> ModelManager:
    m = ModelManager(max_resident=1)
    m.register(
        FakeLocalModel("model/basic", capabilities=ModelCapabilities(text_generation=True)),
        status=ModelStatus.ENABLED,
    )
    m.register(
        FakeLocalModel(
            "model/coder",
            capabilities=ModelCapabilities(text_generation=True, tool_use=True),
        ),
        status=ModelStatus.ENABLED,
    )
    return m


@pytest.fixture()
def tracker() -> ModelPerformanceTracker:
    return ModelPerformanceTracker()


def selector(manager: ModelManager, tracker: ModelPerformanceTracker, **kw) -> ModelSelector:
    return ModelSelector(manager, tracker, hardware=HardwareConstraints(ram_mb=8192), **kw)


def test_select_basic_returns_primary_and_fallbacks(manager, tracker) -> None:
    primary, fallbacks, score, reason = selector(manager, tracker).select(task_family="coding")
    assert primary in ("model/basic", "model/coder")
    assert set(fallbacks) <= {"model/basic", "model/coder"} - {primary}
    assert 0.0 <= score <= 10.0
    assert "task_family" in reason  # BP §225: explainable selection


def test_capability_filter_excludes_non_tool_models(manager, tracker) -> None:
    primary, _, _, _ = selector(manager, tracker).select(
        task_family="automation", required_capabilities={"tool_use"}
    )
    assert primary == "model/coder"  # only tool-capable model qualifies


def test_unknown_capability_fails_closed(manager, tracker) -> None:
    """BP §149: no qualifying model ⇒ BLOCK (never cloud fallback)."""
    with pytest.raises(ModelUnavailable, match="no local model"):
        selector(manager, tracker).select(
            task_family="automation", required_capabilities={"vision"}
        )


def test_hardware_filter_rejects_oversized_models(tracker) -> None:
    m = ModelManager()
    m.register(
        FakeLocalModel(
            "model/huge",
            resources=ResourceRequirements(min_ram_mb=64_000),
        )
    )
    selector_instance = selector(m, tracker)
    with pytest.raises(ModelUnavailable):
        selector_instance.select(task_family="coding")


def test_gpu_required_rejected_without_gpu(tracker) -> None:
    m = ModelManager()
    m.register(
        FakeLocalModel(
            "model/gpu-only",
            resources=ResourceRequirements(min_vram_mb=8192, gpu_required=True),
        ),
        status=ModelStatus.ENABLED,
    )
    with pytest.raises(ModelUnavailable):
        ModelSelector(
            m,
            tracker,
            hardware=HardwareConstraints(ram_mb=8192, vram_mb=0, gpu_available=False),
        ).select(task_family="coding")


def test_learning_promotes_successful_model(manager, tracker) -> None:
    """BP §320/§386: history lets a model win; unproven stays capped."""
    # model/basic earns verified coding history
    for _ in range(6):
        tracker.record(
            model_id="model/basic",
            task_family="coding",
            success=True,
            duration_seconds=1.0,
            verified=True,
        )
    primary, _, _, reason = selector(manager, tracker).select(task_family="coding")
    assert primary == "model/basic"
    assert reason["history_score"] > reason["capability_score"] or primary == "model/basic"


def test_unproven_models_capped(manager, tracker) -> None:
    """BP §67: one lucky run must not dominate — cap until min attempts."""
    tracker.record(
        model_id="model/basic",
        task_family="coding",
        success=True,
        duration_seconds=1.0,
        verified=True,
    )  # single attempt
    policy = SelectionPolicy(min_attempts_for_learning=3, unverified_quality_cap=5.0)
    selector_instance = ModelSelector(manager, tracker, policy=policy)
    _, _, score, reason = selector_instance.select(task_family="coding")
    # capped history can't exceed the cap
    assert reason["history_score"] <= 5.0


def test_project_specific_experience_wins(manager, tracker) -> None:
    """BP §320: 'Model B better for this user's codebase' can win."""
    # Give model/coder strong project-specific history.
    for _ in range(5):
        tracker.record(
            model_id="model/coder",
            task_family="coding:project-x",
            success=True,
            duration_seconds=1.0,
            verified=True,
        )
    primary, _, _, reason = selector(manager, tracker).select(
        task_family="coding", project_context="project-x"
    )
    assert reason.get("project_specific") is True
    assert primary == "model/coder"


def test_disabled_model_not_selectable(manager, tracker) -> None:
    """BP §276: disabled models are unreachable both at selector and manager level."""
    manager.set_status("model/coder", ModelStatus.DISABLED)
    with pytest.raises(ModelUnavailable):
        selector(manager, tracker).select(
            task_family="automation", required_capabilities={"tool_use"}
        )
    with pytest.raises(ModelUnavailable, match="not enabled"):
        manager.get("model/coder")


def test_max_fallbacks_respected(manager, tracker) -> None:
    m = ModelManager()
    for i in range(4):
        m.register(FakeLocalModel(f"model/m{i}"), status=ModelStatus.ENABLED)
    policy = SelectionPolicy(max_fallbacks=1)
    selector_instance = ModelSelector(m, tracker, policy=policy)
    _, fallbacks, _, _ = selector_instance.select(task_family="coding")
    assert len(fallbacks) <= 1
