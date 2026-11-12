"""EngineStructuredGenerator: InferenceEngine -> brain StructuredGenerator
adapter (Sprint 2 model boundary; prompts live ONLY here)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from nomadicos.brain.cognition import CognitiveRequest, CognitiveRole
from nomadicos.inference.mock import MockEngine
from nomadicos.inference.structured import (
    EngineStructuredGenerator,
    _actor_payload_schema,
    _role_schema,
)
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


# ---------------------------------------------------------------------------
# Regression: role payload schemas constrain generation (not validation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "contract_path"),
    [
        (CognitiveRole.ACTOR, "nomadicos.brain.actor:ProposalPayload"),
        (CognitiveRole.DECOMPOSER, "nomadicos.brain.decomposer:DecompositionPayload"),
        (CognitiveRole.PREDICTOR, "nomadicos.brain.deliberation:PredictionPayload"),
        (CognitiveRole.EVALUATOR, "nomadicos.brain.deliberation:EvaluationPayload"),
    ],
)
async def test_adapter_attaches_role_payload_schema(
    role: CognitiveRole, contract_path: str
) -> None:
    """The attached response schema must be exactly the JSON Schema of the
    closed brain payload contract for the role (single source of truth)."""
    module_name, class_name = contract_path.split(":")
    contract = getattr(importlib.import_module(module_name), class_name)

    engine = MockEngine(default_response="{}")
    adapter = EngineStructuredGenerator(engine, "mock-model")
    await adapter.generate_structured(
        CognitiveRequest(role=role, mission_id="mission_m", payload={"k": 1})
    )

    assert engine.calls[0].response_schema == contract.model_json_schema()


async def test_adapter_monitor_role_has_no_schema_and_stays_prompt_only() -> None:
    engine = MockEngine(default_response='{"verdicts": []}')
    adapter = EngineStructuredGenerator(engine, "mock-model")
    await adapter.generate_structured(
        CognitiveRequest(role=CognitiveRole.MONITOR, mission_id="mission_m", payload={})
    )

    assert engine.calls[0].response_schema is None


async def test_adapter_can_disable_schema_constraining() -> None:
    engine = MockEngine(default_response="{}")
    adapter = EngineStructuredGenerator(engine, "mock-model", constrain_to_schema=False)
    await adapter.generate_structured(
        CognitiveRequest(role=CognitiveRole.PREDICTOR, mission_id="mission_m", payload={})
    )

    assert engine.calls[0].response_schema is None


async def test_role_schema_cache_returns_same_object() -> None:
    assert _role_schema("PREDICTOR") is _role_schema("PREDICTOR")
    assert _role_schema("MONITOR") is None
    assert _role_schema("NO_SUCH_ROLE") is None


async def test_actor_schema_narrowed_to_registered_capabilities() -> None:
    """Regression: with a capability list in the payload, the generated
    candidate capability/tool fields are enum-constrained to the registered
    names (generation guidance only; validation is unchanged)."""
    base = _role_schema("ACTOR")
    narrowed = _actor_payload_schema({"capabilities": ["filesystem.write", "filesystem.read"]})

    assert narrowed is not None and narrowed != base
    spec = narrowed["$defs"]["CandidateSpec"]["properties"]
    assert spec["capability"]["enum"] == ["filesystem.write", "filesystem.read"]
    assert spec["tool"]["enum"] == ["filesystem"]
    required = narrowed["$defs"]["CandidateSpec"]["required"]
    assert {"capability", "tool", "arguments"} <= set(required)
    assert "capability" not in _role_schema("ACTOR")["$defs"]["CandidateSpec"].get("required", [])
    # the cached base schema must never be mutated in place
    assert "enum" not in _role_schema("ACTOR")["$defs"]["CandidateSpec"]["properties"]["capability"]


async def test_actor_schema_unchanged_without_capability_list() -> None:
    payload = {"goal_objective": "x"}
    assert _actor_payload_schema(payload) is _role_schema("ACTOR")
    assert _actor_payload_schema({"capabilities": []}) is _role_schema("ACTOR")
    assert _actor_payload_schema({"capabilities": [42, ""]}) is _role_schema("ACTOR")
