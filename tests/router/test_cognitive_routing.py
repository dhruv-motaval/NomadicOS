"""Sprint 7 — cognitive-role routing: capability-aware, deterministic,
fail-safe selection over the EXISTING registry/selector stack."""

from __future__ import annotations

from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.models import RoleModelProfile, parse_role_selection, role_profile
from nomadicos.contracts.model import ModelHealth, ModelRecord
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.cognitive import CognitiveRoleRouter
from nomadicos.router.selection import ModelSelector


def registry_with(*records: ModelRecord) -> ModelRegistry:
    registry = ModelRegistry(EventLogger())
    for record in records:
        registry.register(record)
    return registry


def record(
    model_id: str,
    *,
    roles=None,
    capabilities=None,
    context=8192,
    health=ModelHealth.HEALTHY,
    enabled=True,
) -> ModelRecord:
    return ModelRecord(
        model_id=model_id,
        engine="mock",
        roles=roles or ["worker"],
        capabilities=capabilities or ["text"],
        context_window=context,
        health=health,
        enabled=enabled,
    )


def router_with(registry: ModelRegistry) -> CognitiveRoleRouter:
    return CognitiveRoleRouter(ModelSelector(RouterConfig()), registry)


def test_role_specific_selection_prefers_role_fit() -> None:
    registry = registry_with(
        record("tiny-m", roles=["tiny"], capabilities=["text"]),
        record("reasoner-m", roles=["reasoning", "critic"], capabilities=["text", "reasoning"]),
    )
    data = router_with(registry).select_for_role(role_profile(CognitiveRole.DECOMPOSER))
    assert data["model_id"] == "reasoner-m"  # reasoning role-fit beats smaller tiny model
    selection = parse_role_selection(data, profile=role_profile(CognitiveRole.DECOMPOSER))
    assert selection.available and selection.model_id == "reasoner-m"


def test_capability_aware_selection_not_global_score() -> None:
    """A model strong at planning but WITHOUT the required tool capability
    must not win a role that requires tool reasoning."""
    registry = registry_with(
        record("planner-m", roles=["reasoning"], capabilities=["text", "reasoning"]),
        record("tooler-m", roles=["worker"], capabilities=["text", "tool_use"]),
    )
    data = router_with(registry).select_for_role(
        RoleModelProfile(role=CognitiveRole.ACTOR, required_capabilities=["text", "tool_use"])
    )
    assert data["model_id"] == "tooler-m"  # capability filter, not global score


def test_selection_is_deterministic_with_stable_tiebreak() -> None:
    registry = registry_with(
        record("b-m", roles=["reasoning"], capabilities=["text", "reasoning"]),
        record("a-m", roles=["reasoning"], capabilities=["text", "reasoning"]),
    )
    router = router_with(registry)
    profile = role_profile(CognitiveRole.EVALUATOR)
    first = router.select_for_role(profile)
    second = router.select_for_role(profile)
    assert first == second  # deterministic
    assert first["model_id"] == "a-m"  # tie broken by deterministic size/model_id order


def test_unhealthy_and_disabled_models_excluded() -> None:
    registry = registry_with(
        record(
            "down-m",
            roles=["reasoning"],
            capabilities=["text", "reasoning"],
            health=ModelHealth.UNHEALTHY,
        ),
        record("off-m", roles=["reasoning"], capabilities=["text", "reasoning"], enabled=False),
        record("ok-m", roles=["reasoning"], capabilities=["text", "reasoning"]),
    )
    data = router_with(registry).select_for_role(role_profile(CognitiveRole.PREDICTOR))
    assert data["model_id"] == "ok-m"  # only PROVEN-runnable state is routable
    assert data["engine"] == "mock"
    # unavailable and disabled models never appear as the winner
    assert data["model_id"] not in ("down-m", "off-m")


def test_insufficient_context_rejected_fail_safe() -> None:
    registry = registry_with(
        record(
            "small-m",
            roles=["reasoning"],
            capabilities=["text", "reasoning"],
            context=4096,
        )
    )
    data = router_with(registry).select_for_role(
        RoleModelProfile(
            role=CognitiveRole.PREDICTOR,
            required_capabilities=["text", "reasoning"],
            min_context_tokens=128000,
        )
    )
    assert data["available"] is False
    assert data["model_id"] == ""  # no unsafe selection
    assert "unavailable" in data["reasons"][0]


def test_no_model_at_all_fails_safe() -> None:
    data = router_with(registry_with()).select_for_role(role_profile(CognitiveRole.MONITOR))
    assert data["available"] is False and data["model_id"] == ""


def test_malformed_profile_fails_safe() -> None:
    class Broken:
        role = "NOT_A_ROLE"
        required_capabilities = ["\x00"]
        min_context_tokens = "abc"  # type: ignore[assignment]
        latency_mode = None
        needs_structured_output = True

    data = router_with(registry_with(record("m"))).select_for_role(Broken())
    assert data["available"] is False  # deterministic fail-safe answer, no raise


def test_explanation_metadata_is_complete_and_secret_free() -> None:
    registry = registry_with(
        record("winner-m", roles=["reasoning"], capabilities=["text", "reasoning"]),
        record("loser-m", roles=["tiny"], capabilities=["text"]),
    )
    data = router_with(registry).select_for_role(role_profile(CognitiveRole.DECOMPOSER))
    assert any(r.startswith("role=DECOMPOSER") for r in data["reasons"])
    assert any("required_capabilities" in r for r in data["reasons"])
    assert any("context_floor" in r for r in data["reasons"])
    blob = str(data)
    for forbidden in ("api_key", "password", "secret", "token="):
        assert forbidden not in blob.lower()
