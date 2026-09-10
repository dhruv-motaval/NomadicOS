"""Agents for the model pipeline: selection + handling.

Every step in NomadicOS is an agent with an explicit, auditable decision
(BP §364: per-agent attribution). These two wrap the mechanical selector and
model manager so the pipeline reads as agents:

- SelectorAgent: goal → task family → model choice (+ fallbacks), with the
  decision reason attached to the audit trail.
- ModelHandlerAgent: model_id → loaded LocalModel, handling residency
  (ensure_loaded with bounded wait) and unloading on emergencies.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nomadicos.agent.runtime import _ACTION_VERB, AgentRuntime
from nomadicos.core.logging import get_logger

logger = get_logger("agent.pipeline")

_TASK_FAMILY_WORDS = {
    "coding": ("code", "script", "function", "program", "debug", "refactor"),
    "reasoning": ("why", "reason", "explain", "compare", "analyze", "plan"),
}


@dataclass
class SelectionDecision:
    goal: str
    task_family: str
    model_id: str
    fallbacks: list[str]
    score: float
    reason: dict[str, Any]


class SelectorAgent:
    """Decides which model serves a goal (BP §97, §320) — as an agent."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self._rt = runtime

    @staticmethod
    def task_family(goal: str) -> str:
        """Cheap keyword routing: automation (action verbs) > coding/reasoning
        words > general chat. The selector scores candidates inside the family."""
        text = goal.lower()
        if _ACTION_VERB.search(text) or any(
            w in text for w in _TASK_FAMILY_WORDS["coding"]
        ):
            return "automation"
        if any(w in text for w in _TASK_FAMILY_WORDS["reasoning"]):
            return "reasoning"
        return "general"

    async def select(self, goal: str, *, pinned: str | None = None) -> SelectionDecision:
        if pinned:
            return SelectionDecision(
                goal=goal,
                task_family="general",
                model_id=pinned,
                fallbacks=[],
                score=0.0,
                reason={"pinned": True},
            )
        family = self.task_family(goal)
        model_id, fallbacks, score, reason = self._rt._selector.select(
            task_family=family
        )
        decision = SelectionDecision(
            goal=goal,
            task_family=family,
            model_id=model_id,
            fallbacks=fallbacks,
            score=score,
            reason=reason,
        )
        logger.info(
            "selector agent: goal=%r family=%s model=%s score=%s",
            goal[:60],
            family,
            model_id,
            score,
        )
        return decision

    # ----------------------------------------------------- role-aware choice

    async def strongest(self, *, tool_use: bool = False) -> str:
        """The most capable model available (size-based proxy until Phase 13
        benchmarks) — used to ESCALATE on attempt 2 when the family default
        was insufficient (owner spec: switch to more powerful when needed)."""
        import re as _re

        from nomadicos.core.errors import ModelUnavailable

        best: str | None = None
        best_score = -1.0
        for d in self._rt._manager.list_available():
            if tool_use and not d.capabilities.tool_use:
                continue
            size_match = _re.search(r"(\d+(?:\.\d+)?)b\b", d.model_id.lower())
            params = float(size_match.group(1)) if size_match else 7.0
            score = params + (2 if d.capabilities.tool_use else 0)
            if score > best_score:
                best, best_score = d.model_id, score
        if best is None:
            raise ModelUnavailable("no local models available")
        return best

    async def select_for_role(self, goal: str, role_name: str) -> SelectionDecision:
        """Per-agent model choice (BP §364): planner/synthesizer need the
        strongest reasoner (reasoning family); workers follow the goal."""
        if role_name in ("planner", "synthesizer"):
            model_id, fallbacks, score, reason = self._rt._selector.select(
                task_family="reasoning"
            )
            return SelectionDecision(
                goal=goal,
                task_family="reasoning",
                model_id=model_id,
                fallbacks=fallbacks,
                score=score,
                reason=reason,
            )
        return await self.select(goal)


class ModelHandlerAgent:
    """Loads/unloads models for the pipeline (ADR-0006 residency, I10 budgets)
    — as an agent with bounded waits."""

    def __init__(self, runtime: AgentRuntime, *, load_timeout_s: float = 120.0) -> None:
        self._rt = runtime
        self._load_timeout_s = load_timeout_s

    async def ensure_model(self, model_id: str) -> Any:
        return await asyncio.wait_for(
            self._rt._manager.ensure_loaded(model_id), timeout=self._load_timeout_s
        )

    async def release(self, model_id: str) -> None:
        try:
            await self._rt._manager.unload(model_id)
        except Exception:  # noqa: BLE001 — unload is best effort
            logger.debug("unload failed model=%s", model_id, exc_info=True)


__all__ = ["ModelHandlerAgent", "SelectionDecision", "SelectorAgent"]
