import pytest

from nomadicos.models.base import ModelStatus
from nomadicos.models.fake import FakeLocalModel, fake_vision_model


def test_fake_model_lifecycle() -> None:
    model = FakeLocalModel()
    assert not model.is_loaded
    import asyncio

    asyncio.run(model.load())
    assert model.is_loaded
    asyncio.run(model.unload())
    assert not model.is_loaded
    assert model.load_calls == 1 and model.unload_calls == 1


async def test_fake_model_generate() -> None:
    model = FakeLocalModel(responses=["hello world"])
    await model.load()
    request = __import__("nomadicos.models.base", fromlist=["GenerateRequest"]).GenerateRequest(
        prompt="say hello", max_output_tokens=32
    )
    result = await model.generate(request)
    assert result.text == "hello world"
    assert model.generate_calls == [request]


async def test_fake_model_requires_load() -> None:
    from nomadicos.models.base import GenerateRequest

    model = FakeLocalModel()
    with pytest.raises(RuntimeError, match="not loaded"):
        await model.generate(GenerateRequest(prompt="hi"))


def test_fake_vision_model_has_vision_capability() -> None:
    model = fake_vision_model()
    assert model.capabilities.vision is True
    import asyncio

    description = asyncio.run(model.describe(b"\x89PNG-data", "what is this?"))
    assert description.startswith("vision-description:")


def test_descriptor_defaults() -> None:
    model = FakeLocalModel(model_id="fake/x")
    assert model.descriptor.status is ModelStatus.VALIDATED
    assert model.resource_requirements.min_ram_mb == 64
