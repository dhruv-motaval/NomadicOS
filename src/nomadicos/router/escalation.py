"""Bounded escalation (SPEC §14): small models may fail, then escalate.

Triggers are recorded by the orchestrator; escalation walks the
smallest→larger qualified chain and never runs forever.
"""

from __future__ import annotations

from nomadicos.contracts.model import ModelRecord, TaskRequirements
from nomadicos.kernel.errors import BudgetExhausted
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.selection import ModelSelector


class EscalationPolicy:
    def __init__(self, selector: ModelSelector, max_escalations: int) -> None:
        self._selector = selector
        self._max = max_escalations

    def next_model(
        self,
        registry: ModelRegistry,
        requirements: TaskRequirements,
        tried: list[str],
        escalation_count: int,
    ) -> ModelRecord:
        if escalation_count >= self._max:
            raise BudgetExhausted(
                f"escalation limit reached after {escalation_count} attempts (SPEC §14)",
                tried=tried,
                max_escalations=self._max,
            )
        ranked = self._selector.rank(registry, requirements)
        for candidate in ranked:
            if candidate.model_id not in tried:
                return registry.get(candidate.model_id)
        raise BudgetExhausted("no untried qualified model remains", tried=tried)
