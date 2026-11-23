"""retry_tried_models: a single-model pool may re-run its only model (bounded)."""

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
from nomadicos.kernel.errors import BudgetExhausted
from nomadicos.kernel.events import EventLogger
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.escalation import EscalationPolicy
from nomadicos.router.selection import ModelSelector

REQS = TaskRequirements(
    task_type=TaskType.SIMPLE_FILE,
    capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE],
    difficulty=0.2,
)


def registry(*ids: str) -> ModelRegistry:
    reg = ModelRegistry(EventLogger())
    for i, model_id in enumerate(ids):
        reg.register(
            ModelRecord(
                model_id=model_id,
                engine="mock",
                capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE],
                health=ModelHealth.HEALTHY,
                params={"size_hint_b": float(i + 1)},
            )
        )
    return reg


def policy(max_escalations: int, retry: bool) -> EscalationPolicy:
    return EscalationPolicy(ModelSelector(RouterConfig()), max_escalations, retry_tried=retry)


def test_default_still_refuses_to_repeat_the_only_model() -> None:
    with pytest.raises(BudgetExhausted, match="no untried"):
        policy(3, retry=False).next_model(
            registry("only"), REQS, tried=["only"], escalation_count=1
        )


def test_single_model_pool_can_retry_itself_but_only_within_the_escalation_budget() -> None:
    reg, pol = registry("only"), policy(2, retry=True)
    assert pol.next_model(reg, REQS, tried=["only"], escalation_count=1).model_id == "only"
    with pytest.raises(BudgetExhausted, match="escalation limit"):
        pol.next_model(reg, REQS, tried=["only", "only"], escalation_count=2)


def test_untried_models_still_come_first_then_least_recently_tried() -> None:
    reg, pol = registry("a", "b"), policy(6, retry=True)
    assert pol.next_model(reg, REQS, tried=["a"], escalation_count=1).model_id == "b"
    # both failed: "a" failed longer ago than "b", so it goes first again
    assert pol.next_model(reg, REQS, tried=["a", "b"], escalation_count=2).model_id == "a"
    assert pol.next_model(reg, REQS, tried=["b", "a"], escalation_count=2).model_id == "b"


def test_nothing_qualified_still_fails_closed_even_with_retry_on() -> None:
    empty = ModelRegistry(EventLogger())
    with pytest.raises(BudgetExhausted, match="no untried"):
        policy(3, retry=True).next_model(empty, REQS, tried=["gone"], escalation_count=1)
