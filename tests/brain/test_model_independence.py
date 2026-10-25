"""Sprint 7 — model independence: the brain's ONLY model knowledge is a
deterministic capability profile behind a protocol. No provider names,
no clients, no registry/router imports, fail-closed selection parsing."""

from __future__ import annotations

import re

import pytest

from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.models import (
    RoleModelProfile,
    RoleModelRouter,
    RoleModelSelection,
    parse_role_selection,
    role_profile,
)
from nomadicos.brain.state import BrainState


def test_role_profiles_are_deterministic_per_role() -> None:
    for role in CognitiveRole:
        assert role_profile(role) == role_profile(role)  # deterministic
    actor = role_profile(CognitiveRole.ACTOR)
    decomposer = role_profile(CognitiveRole.DECOMPOSER)
    assert "tool_use" in actor.required_capabilities  # actor reasons about tools
    assert "reasoning" in decomposer.required_capabilities
    assert actor.needs_structured_output  # every brain role parses closed JSON


def test_role_profile_contract_is_closed_and_bounded() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        RoleModelProfile(role=CognitiveRole.ACTOR, required_capabilities=["text", "text"])
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        RoleModelProfile(
            role=CognitiveRole.ACTOR,
            required_capabilities=["text"],
            min_context_tokens=2_000_000,
        )
    # authority-shaped extras are structurally impossible (closed schema)
    profile = role_profile(CognitiveRole.ACTOR).model_dump(mode="json")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        RoleModelProfile.model_validate({**profile, "authorized": True})


def test_selection_parsing_fails_closed() -> None:
    profile = role_profile(CognitiveRole.PREDICTOR)
    good = parse_role_selection(
        {
            "role": "PREDICTOR",
            "model_id": "m1",
            "engine": "e1",
            "score": 0.9,
            "reasons": ["measured"],
            "rejected": ["m2: weak"],
        },
        profile=profile,
    )
    assert good.model_id == "m1" and good.available
    # malformed: missing score
    with pytest.raises(ModelOutputRejected):
        parse_role_selection({"role": "PREDICTOR", "model_id": "m1"}, profile=profile)
    # authority-shaped extra
    with pytest.raises(ModelOutputRejected):
        parse_role_selection(
            {
                "role": "PREDICTOR",
                "model_id": "m1",
                "engine": "e1",
                "score": 0.9,
                "authorized": True,
            },
            profile=profile,
        )
    # unavailable selection must not carry a model id
    with pytest.raises(ModelOutputRejected):
        parse_role_selection(
            {"role": "PREDICTOR", "model_id": "m1", "engine": "", "score": 0.0, "available": False},
            profile=profile,
        )
    # out-of-range score
    with pytest.raises(ModelOutputRejected):
        parse_role_selection(
            {"role": "PREDICTOR", "model_id": "m1", "engine": "e1", "score": 1.5},
            profile=profile,
        )


def test_role_model_router_is_a_structural_protocol() -> None:
    class Adapter:
        def select_for_role(self, profile: RoleModelProfile) -> dict:
            return {
                "role": profile.role.value,
                "model_id": "m",
                "engine": "e",
                "score": 0.5,
                "available": True,
            }

    assert isinstance(Adapter(), RoleModelRouter)  # satisfied structurally


def test_brain_modules_import_no_provider_router_or_registry() -> None:
    import pathlib

    brain_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "nomadicos" / "brain"
    for path in sorted(brain_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(?:from|import)\s+nomadicos\.(?:registry|router|inference|persistence)",
            source,
            re.M,
        ), f"{path.name} imports a model-infrastructure brick"


def test_sprint7_module_has_no_provider_specific_logic() -> None:
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2] / "src" / "nomadicos" / "brain" / "models.py"
    ).read_text(encoding="utf-8")
    for provider in ("glm", "nvidia", "ollama", "openai", "anthropic", "qwen", "llama"):
        assert provider not in source.lower(), provider
    # no conditional on any engine/provider string
    assert not re.search(r'if\s+.*(?:engine|provider)\s*(?:==|in)\s*"', source, re.I)


def test_brainstate_never_contains_live_clients() -> None:
    state = BrainState.for_goal(
        __import__("nomadicos.contracts.core", fromlist=["Goal"]).Goal(
            id="task_g", objective="obj", predicates=[]
        )
    )
    dump = state.model_dump(mode="json")
    blob = str(dump)
    for forbidden in ("client", "http", "api_key", "token=", "session"):
        assert forbidden not in blob.lower(), forbidden
    # Sprint 7 adds no model fields to BrainState: routing stays outside
    assert not any("model" in key for key in dump)


def test_selection_is_data_only() -> None:
    selection = RoleModelSelection(role=CognitiveRole.ACTOR, model_id="m", engine="e", score=0.5)
    dump = selection.model_dump(mode="json")
    forbidden_fields = (
        "authorized",
        "allowed",
        "approved",
        "permission",
        "bypass",
        "secret",
        "api_key",
    )
    for forbidden in forbidden_fields:
        assert forbidden not in dump
