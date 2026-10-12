"""EngineStructuredGenerator: InferenceEngine -> brain StructuredGenerator
adapter (Sprint 2 model boundary; prompts live ONLY here)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nomadicos.brain.cognition import CognitiveRequest, CognitiveRole
from nomadicos.inference.mock import MockEngine
from nomadicos.inference.structured import EngineStructuredGenerator
from nomadicos.kernel.errors import ModelError


async def test_adapter_routes_roles_to_engine_and_returns_text() -> None:
    engine = MockEngine(default_response='{"verdicts": []}')
    adapter = EngineStructuredGenerator(engine, "mock-model")
    request = CognitiveRequest(role=CognitiveRole.MONITOR, mission_id="mission_m", payload={"k": 1})

    text = await adapter.generate_structured(request)

    assert text == '{"verdicts": []}'
    sent = engine.calls[0]
    assert sent.model_id == "mock-model"
    prompt = sent.messages[0].content
    assert "semantic monitor" in prompt
    assert '"payload"' in prompt and '"k"' in prompt


async def test_adapter_prompt_contains_typed_payload() -> None:
    engine = MockEngine(default_response="{}")
    adapter = EngineStructuredGenerator(engine, "mock-model")
    request = CognitiveRequest(
        role=CognitiveRole.DECOMPOSER,
        mission_id="mission_d",
        payload={"goal": {"id": "task_g", "objective": "Produce out.txt"}},
    )

    await adapter.generate_structured(request)

    prompt = engine.calls[0].messages[0].content
    assert "task_g" in prompt
    assert "mission_d" in prompt


async def test_unknown_role_fails_closed() -> None:
    engine = MockEngine(default_response="{}")
    adapter = EngineStructuredGenerator(engine, "mock-model")
    stranger = SimpleNamespace(role="MYSTERY", mission_id="m", payload={})

    with pytest.raises(ModelError, match="unknown cognitive role"):
        await adapter.generate_structured(stranger)
