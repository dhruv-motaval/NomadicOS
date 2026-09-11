import pytest

from nomadicos.core.errors import ModelResourceError, ModelUnavailable
from nomadicos.models.base import GenerateRequest, ModelStatus, ResourceRequirements
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.manager import ModelManager


def make_manager(**kwargs) -> ModelManager:
    return ModelManager(**kwargs)


def test_register_and_list() -> None:
    manager = make_manager()
    a = FakeLocalModel("fake/a")
    b = FakeLocalModel("fake/b")
    manager.register(a, status=ModelStatus.ENABLED)
    manager.register(b, status=ModelStatus.ENABLED)
    ids = [d.model_id for d in manager.list_available()]
    assert ids == ["fake/a", "fake/b"]


def test_duplicate_registration_rejected() -> None:
    manager = make_manager()
    manager.register(FakeLocalModel("fake/a"))
    with pytest.raises(ValueError, match="already registered"):
        manager.register(FakeLocalModel("fake/a"))


def test_unknown_and_disabled_models_rejected() -> None:
    manager = make_manager()
    manager.register(FakeLocalModel("fake/a"), status=ModelStatus.DISABLED)
    with pytest.raises(ModelUnavailable):
        manager.get("fake/missing")
    with pytest.raises(ModelUnavailable, match="not enabled"):
        manager.get("fake/a")


@pytest.mark.asyncio
async def test_sequential_residency_swaps_models() -> None:
    """ADR-0006: at most 1 resident; loading B unloads A."""
    manager = make_manager(max_resident=1)
    a = FakeLocalModel("fake/a")
    b = FakeLocalModel("fake/b")
    manager.register(a, status=ModelStatus.ENABLED)
    manager.register(b, status=ModelStatus.ENABLED)

    await manager.ensure_loaded("fake/a")
    assert manager.resident == ["fake/a"]
    assert a.is_loaded and not b.is_loaded

    await manager.ensure_loaded("fake/b")
    assert manager.resident == ["fake/b"]
    assert not a.is_loaded and b.is_loaded
    assert a.unload_calls == 1


@pytest.mark.asyncio
async def test_ensure_loaded_is_idempotent() -> None:
    manager = make_manager(max_resident=1)
    a = FakeLocalModel("fake/a")
    manager.register(a, status=ModelStatus.ENABLED)
    await manager.ensure_loaded("fake/a")
    await manager.ensure_loaded("fake/a")
    assert a.load_calls == 1
    assert manager.resident == ["fake/a"]


@pytest.mark.asyncio
async def test_generate_requires_residency() -> None:
    manager = make_manager()
    a = FakeLocalModel("fake/a")
    manager.register(a, status=ModelStatus.ENABLED)
    with pytest.raises(ModelResourceError, match="not resident"):
        await manager.generate("fake/a", GenerateRequest(prompt="hi"))
    await manager.ensure_loaded("fake/a")
    result = await manager.generate("fake/a", GenerateRequest(prompt="hi"))
    assert result.text == "ok"


@pytest.mark.asyncio
async def test_unload_frees_residency() -> None:
    manager = make_manager(max_resident=1)
    a = FakeLocalModel("fake/a")
    manager.register(a, status=ModelStatus.ENABLED)
    await manager.ensure_loaded("fake/a")
    await manager.unload("fake/a")
    assert manager.resident == []
    assert not a.is_loaded


def test_load_cost_zero_when_resident() -> None:
    manager = make_manager(max_resident=1)
    a = FakeLocalModel(
        "fake/a", resources=ResourceRequirements(estimated_load_seconds=2.5)
    )
    manager.register(a, status=ModelStatus.ENABLED)
    assert manager.load_cost("fake/a") == 2.5
