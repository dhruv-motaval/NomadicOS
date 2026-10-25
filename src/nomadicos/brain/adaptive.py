"""AdaptiveController — ONE deterministic adaptive-intelligence boundary
(Sprint 6, spec §8-9): attention → beliefs → metacognition → information
needs → effort plan, in one pass over a BrainState.

Cognitive flow position (spec §4 flow, extended)::

    Goal → Memory Retrieval (Sprint 5 protocol seam, optional)
         → ATTENTION (this module)
         → Beliefs/Uncertainty → Metacognition (advisory)
         → Information Needs → Effort Plan
         → Decomposer / Actor / Predictor / Evaluator (unchanged)
         → Reality Loop (unchanged)

Hard rules:

- DATA only: the controller updates frozen BrainState copies with the
  new adaptive fields and appends trace events. It CANNOT authorize,
  execute, verify, or override any budget — the existing deterministic
  controls (monitor gate, authority, verification, BudgetState) stay
  outside and unweakened.
- Memory enters ONLY through the Sprint 5 ``MemoryController`` seam
  (brain never imports the memory brick; the retriever protocol is
  satisfied structurally). A memory failure is fail-safe: the pass
  continues without memory input.
- Deterministic: fixed weights/thresholds, position-based recency (no
  wall clock in scoring), injectable event ids/timestamps — identical
  inputs replay to identical states.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nomadicos.brain.attention import compute_attention
from nomadicos.brain.effort import decide_effort
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.information import identify_information_needs, to_observe_candidate
from nomadicos.brain.memory import MemoryController
from nomadicos.brain.metacognition import assess_metacognition
from nomadicos.brain.schemas import MAX_INFO_REQUESTS
from nomadicos.brain.state import BrainState
from nomadicos.brain.uncertainty import assess_beliefs
from nomadicos.kernel.ids import new_id


class AdaptiveController:
    """One clear adaptive-intelligence boundary. Sync (no model calls):
    every assessment is a deterministic function of the state."""

    def __init__(
        self,
        *,
        memory: MemoryController | None = None,
        now: Callable[[], datetime] | None = None,
        event_id: Callable[[], str] | None = None,
        events: list[BrainEvent] | None = None,
    ) -> None:
        self._memory = memory
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._event_id = event_id if event_id is not None else (lambda: new_id("evt"))
        self._events = events

    # ------------------------------------------------------------- events ---
    def mint_event(
        self,
        state: BrainState,
        event_type: BrainEventType,
        payload: dict[str, Any],
        *,
        actor: str,
        parent_event_id: str | None,
    ) -> BrainEvent:
        event = BrainEvent(
            event_id=self._event_id(),
            mission_id=state.mission_id,
            timestamp=self._now(),
            event_type=event_type,
            actor=actor,
            parent_event_id=parent_event_id,
            payload=payload,
        )
        if self._events is not None:
            self._events.append(event)
        return event

    # -------------------------------------------------------------- pass ---
    def run(self, state: BrainState, *, extra_query: str = "") -> BrainState:
        """One adaptive pass: memory (optional) → attention → beliefs →
        metacognition → info requests → effort plan. Returns a NEW
        BrainState; the input is never mutated. Fail-safe: each stage is
        independent — a memory failure degrades to memory-less attention."""
        last_event_id: str | None = None

        if self._memory is not None:
            try:
                state = self._memory.into_state(state, extra_query=extra_query)
                last_event_id = state.history[-1].event_id if state.history else None
            except Exception:  # noqa: BLE001 - memory is fail-safe by contract
                state = state

        attention = compute_attention(state)
        attention_event = self.mint_event(
            state,
            BrainEventType.BRAIN_ATTENTION_FOCUSED,
            {
                "item_count": len(attention.items),
                "top_refs": [i.ref for i in attention.items[:4]],
                "top_scores": [i.score for i in attention.items[:4]],
            },
            actor="attention",
            parent_event_id=last_event_id,
        )
        state = state.with_event(attention_event)
        last_event_id = attention_event.event_id

        beliefs = assess_beliefs(state)
        beliefs_event = self.mint_event(
            state,
            BrainEventType.BRAIN_BELIEFS_ASSESSED,
            {
                "overall_uncertainty": beliefs.overall_uncertainty,
                "unknown_keys": list(beliefs.unknown_keys),
                "conflicting_hypotheses": beliefs.conflicting_hypotheses,
                "verified_facts": beliefs.verified_facts,
            },
            actor="uncertainty",
            parent_event_id=last_event_id,
        )
        state = state.with_event(beliefs_event)
        last_event_id = beliefs_event.event_id

        metacognition = assess_metacognition(state, beliefs)
        meta_event = self.mint_event(
            state,
            BrainEventType.BRAIN_METACOGNITION_PRODUCED,
            {
                "recommendation": metacognition.recommendation.value,
                "continue_reasoning": metacognition.continue_reasoning,
                "missing_count": len(metacognition.missing),
            },
            actor="metacognition",
            parent_event_id=last_event_id,
        )
        state = state.with_event(meta_event)
        last_event_id = meta_event.event_id

        requests = identify_information_needs(state, beliefs)[:MAX_INFO_REQUESTS]
        if requests:
            info_event = self.mint_event(
                state,
                BrainEventType.BRAIN_INFO_GATHERING_REQUESTED,
                {
                    "request_ids": [r.id for r in requests],
                    "targets": [r.target for r in requests],
                    "priorities": [r.priority for r in requests],
                },
                actor="information",
                parent_event_id=last_event_id,
            )
            state = state.with_event(info_event)
            last_event_id = info_event.event_id

        effort = decide_effort(state, beliefs, metacognition)
        effort_event = self.mint_event(
            state,
            BrainEventType.BRAIN_EFFORT_DECIDED,
            {
                "decision": effort.decision.value,
                "max_iterations": effort.max_iterations,
                "search_depth": effort.search_depth,
            },
            actor="effort",
            parent_event_id=last_event_id,
        )
        state = state.with_event(effort_event)
        last_event_id = effort_event.event_id

        return state.model_copy(
            update={
                "attention": attention,
                "belief_assessment": beliefs,
                "metacognition": metacognition,
                "information_requests": requests,
                "effort_plan": effort,
                "state_version": state.state_version + 1,
            }
        )

    # ------------------------------------------------------- observables ---
    def observe_candidate(self, state: BrainState, *, subgoal_id: str | None = None):
        """Build the OBSERVE candidate for the TOP information request, if
        any. The candidate travels the NORMAL pipeline (monitor gate →
        Action IR → authority → executor) — nothing is executed here."""
        requests = state.information_requests
        if not requests:
            return None
        return to_observe_candidate(requests[0], subgoal_id=subgoal_id)
