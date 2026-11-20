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
    def __init__(
        self, selector: ModelSelector, max_escalations: int, *, retry_tried: bool = False
    ) -> None:
        self._selector = selector
        self._max = max_escalations
        self._retry_tried = retry_tried

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
                # exact engine identity (Phase 14C-F): the same model_id
                # under several engines never collapses to an arbitrary one
                return registry.get(candidate.model_id, candidate.engine or None)
        if self._retry_tried and ranked:
            # every qualified model failed once (a single-model pool always
            # lands here): the least recently tried goes first again. Still
            # bounded by max_escalations above, never by chance.
            last_try = {model_id: i for i, model_id in enumerate(tried)}
            again = min(ranked, key=lambda c: last_try.get(c.model_id, -1))
            return registry.get(again.model_id, again.engine or None)
        raise BudgetExhausted("no untried qualified model remains", tried=tried)
