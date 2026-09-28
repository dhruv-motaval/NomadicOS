"""MAP core loop — Actor <-> Monitor refinement (NomadicBrain spec §16,
Sprint 2) and the Goal -> decomposition -> proposals facade.

Canonical bounded loop::

    Actor -> candidates -> Monitor
        reject + structured feedback -> Actor refinement (if budget)
        accept -> stop, explicit accepted candidates
    budget exhausted -> structured PlanningFailure

Deterministic control (no model here):

- bounded iterations (``LoopConfig.max_iterations``); deterministic
  stopping condition: stop on the first iteration with >= 1 accepted
  candidate, else when the budget is exhausted — no infinite retry loop;
- structured failure: a ``PlanningOutcome`` with an explicit failure
  reason, never a silent success and never an execution;
- provenance: every candidate keeps its revision/derived_from, every
  monitor result keeps its iteration, and every step emits a
  ``BrainEvent`` (data only) chained by ``parent_event_id``.

Event identity and timestamps are INJECTED (``event_id``/``now``
callables) so identical inputs + identical injections replay to
identical trajectories; defaults mint wall-clock ids. Nothing here
executes tools or produces authority: the loop ends at proposals and
monitor decisions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.actor import Actor, ActorProposal
from nomadicos.brain.decomposer import Decomposer, apply_decomposition
from nomadicos.brain.errors import BrainError
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.monitor import Monitor, MonitorDecision
from nomadicos.brain.schemas import (
    MAX_DESCRIPTION,
    MAX_PROVENANCE_REFS,
    ActionCandidate,
    Decision,
    MonitorResult,
    Subgoal,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id

MAX_ITERATIONS_BOUND = 16

EventClock = Callable[[], datetime]


def _failure_reason(exc: Exception) -> str:
    """Normalize any failure into a bounded, structured reason string."""
    if isinstance(exc, BrainError):
        return exc.message
    return f"{type(exc).__name__}: {exc}"


class LoopConfig(Contract):
    """Bounded refinement budget (deterministic hard control)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: total actor passes (initial proposal + refinements)
    max_iterations: int = Field(default=3, ge=1, le=MAX_ITERATIONS_BOUND)
    #: deterministic per-proposal candidate ceiling
    max_candidates: int = Field(default=8, ge=1, le=64)


class PlanningStage(StrEnum):
    """Where a planning run stopped."""

    DECOMPOSITION = "DECOMPOSITION"
    PROPOSAL = "PROPOSAL"


class PlanningStatus(StrEnum):
    """Outcome of one MAP planning run. No implicit success exists."""

    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"


class PlanningFailure(Contract):
    """Structured planning failure (data, never coerced into success)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: PlanningStage
    reason: str
    iterations_used: int = Field(ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> PlanningFailure:
        if not self.reason.strip():
            raise ValueError("planning failure reason must not be empty")
        if len(self.reason) > MAX_DESCRIPTION:
            raise ValueError(f"planning failure reason exceeds {MAX_DESCRIPTION} chars")
        return self


class PlanningOutcome(Contract):
    """Explicit result of one MAP planning run (data only).

    Neither status executes anything and neither is goal SUCCESS: the
    outcome carries proposals, monitor decisions, and — on failure — a
    structured reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: PlanningStatus
    iterations_used: int = Field(ge=0)
    accepted_candidate_ids: list[str] = Field(default_factory=list)
    monitor_results: list[MonitorResult] = Field(default_factory=list)
    failure: PlanningFailure | None = None

    @model_validator(mode="after")
    def _coherent(self) -> PlanningOutcome:
        if self.status is PlanningStatus.ACCEPTED:
            if self.failure is not None:
                raise ValueError("an ACCEPTED outcome must not carry a failure")
            if not self.accepted_candidate_ids:
                raise ValueError("an ACCEPTED outcome requires an accepted candidate")
        else:
            if self.failure is None or not self.failure.reason.strip():
                raise ValueError("a FAILED outcome requires a structured failure reason")
            if self.accepted_candidate_ids:
                raise ValueError("a FAILED outcome must not carry accepted candidates")
        return self


class ActorMonitorLoop:
    """Bounded Actor <-> Monitor refinement loop (spec §16).

    Stateless controller: every ``run`` is a pure transition of the
    input state. Event ids/timestamps are injectable; with identical
    injections the loop replays deterministically.
    """

    def __init__(
        self,
        config: LoopConfig | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        event_id: Callable[[], str] | None = None,
        events: list[BrainEvent] | None = None,
    ) -> None:
        self._config = config if config is not None else LoopConfig()
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._event_id = event_id if event_id is not None else (lambda: new_id("evt"))
        #: optional caller-owned capture list (trace/tests); events stay DATA
        self._events = events

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

    async def run(
        self,
        state: BrainState,
        subgoal: Subgoal,
        actor: Actor,
        monitor: Monitor,
        *,
        parent_event_id: str | None = None,
    ) -> tuple[BrainState, PlanningOutcome]:
        if subgoal.goal_id != state.goal.id:
            raise BrainError("subgoal does not belong to the state's goal")

        all_candidates: list[ActionCandidate] = []
        all_results: list[MonitorResult] = []
        feedback: list[MonitorResult] = []
        last_event_id = parent_event_id

        for iteration in range(1, self._config.max_iterations + 1):
            try:
                proposal = await actor.propose(state, subgoal, tuple(feedback), iteration)
            except Exception as exc:  # fail closed: actor failure -> structured failure
                reason = _failure_reason(exc)
                return self._fail(
                    state,
                    subgoal,
                    reason,
                    iteration,
                    all_candidates,
                    all_results,
                    parent_event_id=last_event_id,
                )
            proposal = self._truncate(proposal)

            candidates_event = self.mint_event(
                state,
                BrainEventType.BRAIN_CANDIDATES_GENERATED,
                {
                    "subgoal_id": subgoal.id,
                    "iteration": iteration,
                    "candidate_ids": [c.id for c in proposal.candidates],
                    "source": proposal.source,
                },
                actor="actor",
                parent_event_id=last_event_id,
            )
            state = state.with_event(candidates_event)
            last_event_id = candidates_event.event_id

            results = await monitor.evaluate(state, subgoal, proposal, iteration)
            monitor_event = self.mint_event(
                state,
                BrainEventType.BRAIN_MONITOR_EVALUATED,
                {
                    "iteration": iteration,
                    "results": [
                        {
                            "candidate_id": r.candidate_id,
                            "decision": r.decision.value,
                            "stage": r.stage.value,
                            "reasons": list(r.reasons),
                        }
                        for r in results
                    ],
                },
                actor="monitor",
                parent_event_id=last_event_id,
            )
            state = state.with_event(monitor_event)
            last_event_id = monitor_event.event_id

            all_candidates.extend(proposal.candidates)
            all_results.extend(results)

            accepted = [r for r in results if r.decision is MonitorDecision.ACCEPTED]
            for result in results:
                is_accepted = result.decision is MonitorDecision.ACCEPTED
                candidate_event = self.mint_event(
                    state,
                    BrainEventType.BRAIN_CANDIDATE_ACCEPTED
                    if is_accepted
                    else BrainEventType.BRAIN_CANDIDATE_REJECTED,
                    {
                        "candidate_id": result.candidate_id,
                        "iteration": iteration,
                        "stage": result.stage.value,
                        "reasons": list(result.reasons),
                    },
                    actor="monitor",
                    parent_event_id=last_event_id,
                )
                state = state.with_event(candidate_event)
                last_event_id = candidate_event.event_id

            if accepted:
                decisions = [
                    Decision(
                        id=f"dec_{result.candidate_id}",
                        candidate_id=result.candidate_id,
                        selected=True,
                        rationale="accepted by monitor",
                        factors={"iteration": result.iteration, "stage": result.stage.value},
                    )
                    for result in accepted
                ]
                state = self._with_planning(
                    state,
                    candidates=all_candidates,
                    results=all_results,
                    decisions=decisions,
                    iterations_used=iteration,
                )
                return state, PlanningOutcome(
                    status=PlanningStatus.ACCEPTED,
                    iterations_used=iteration,
                    accepted_candidate_ids=[r.candidate_id for r in accepted],
                    monitor_results=all_results,
                )

            if iteration < self._config.max_iterations:
                feedback = [r for r in results if r.decision is MonitorDecision.REJECTED]
                refinement_event = self.mint_event(
                    state,
                    BrainEventType.BRAIN_ACTOR_REFINEMENT,
                    {
                        "next_iteration": iteration + 1,
                        "feedback_candidate_ids": [r.candidate_id for r in feedback][
                            :MAX_PROVENANCE_REFS
                        ],
                    },
                    actor="loop",
                    parent_event_id=last_event_id,
                )
                state = state.with_event(refinement_event)
                last_event_id = refinement_event.event_id

        return self._fail(
            state,
            subgoal,
            "retry budget exhausted: every candidate was rejected",
            self._config.max_iterations,
            all_candidates,
            all_results,
            parent_event_id=last_event_id,
        )

    # --------------------------------------------------------- internals ---

    def _truncate(self, proposal: ActorProposal) -> ActorProposal:
        if len(proposal.candidates) <= self._config.max_candidates:
            return proposal
        return proposal.model_copy(
            update={"candidates": proposal.candidates[: self._config.max_candidates]}
        )

    def _fail(
        self,
        state: BrainState,
        subgoal: Subgoal,
        reason: str,
        iterations_used: int,
        candidates: list[ActionCandidate],
        results: list[MonitorResult],
        *,
        parent_event_id: str | None,
    ) -> tuple[BrainState, PlanningOutcome]:
        failure = PlanningFailure(
            stage=PlanningStage.PROPOSAL,
            reason=reason[:MAX_DESCRIPTION],
            iterations_used=iterations_used,
        )
        failure_event = self.mint_event(
            state,
            BrainEventType.BRAIN_PLANNING_FAILED,
            {
                "stage": failure.stage.value,
                "reason": failure.reason[:500],
                "subgoal_id": subgoal.id,
                "iterations_used": iterations_used,
            },
            actor="loop",
            parent_event_id=parent_event_id,
        )
        state = state.with_event(failure_event)
        state = self._with_planning(
            state, candidates=candidates, results=results, iterations_used=iterations_used
        )
        outcome = PlanningOutcome(
            status=PlanningStatus.FAILED,
            iterations_used=iterations_used,
            accepted_candidate_ids=[],
            monitor_results=results,
            failure=failure,
        )
        return state, outcome

    @staticmethod
    def _with_planning(
        state: BrainState,
        *,
        candidates: list[ActionCandidate],
        results: list[MonitorResult],
        iterations_used: int,
        decisions: Sequence[Decision] = (),
    ) -> BrainState:
        return state.model_copy(
            update={
                "action_candidates": [*state.action_candidates, *candidates],
                "monitor_results": [*state.monitor_results, *results],
                "planning_decisions": [*state.planning_decisions, *decisions],
                "planning_iterations": state.planning_iterations + iterations_used,
                "state_version": state.state_version + 1,
            }
        )


class MAPPlanner:
    """Goal -> decomposition -> Actor/Monitor proposals (Sprint 2 MAP
    facade, spec §4 flow up to monitoring). Deterministic orchestration
    of the decomposer and the refinement loop; no execution, no
    authority."""

    def __init__(
        self,
        loop: ActorMonitorLoop | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        event_id: Callable[[], str] | None = None,
    ) -> None:
        self._loop = loop if loop is not None else ActorMonitorLoop(now=now, event_id=event_id)

    async def plan(
        self,
        state: BrainState,
        decomposer: Decomposer,
        actor: Actor,
        monitor: Monitor,
        *,
        subgoal: Subgoal | None = None,
    ) -> tuple[BrainState, PlanningOutcome]:
        last_event_id: str | None = None

        if not state.task_graph.nodes:
            requested_event = self._loop.mint_event(
                state,
                BrainEventType.BRAIN_DECOMPOSITION_REQUESTED,
                {"goal_id": state.goal.id},
                actor="decomposer",
                parent_event_id=None,
            )
            state = state.with_event(requested_event)
            last_event_id = requested_event.event_id
            try:
                decomposition = await decomposer.decompose(state)
                state = apply_decomposition(state, decomposition)
            except Exception as exc:  # fail closed: decomposition failures are explicit
                reason = _failure_reason(exc)
                failed_event = self._loop.mint_event(
                    state,
                    BrainEventType.BRAIN_DECOMPOSITION_FAILED,
                    {"reason": reason[:500]},
                    actor="decomposer",
                    parent_event_id=last_event_id,
                )
                state = state.with_event(failed_event)
                return self._fail_decomposition(state, reason, failed_event.event_id)

            produced_event = self._loop.mint_event(
                state,
                BrainEventType.BRAIN_DECOMPOSITION_PRODUCED,
                {
                    "node_ids": decomposition.task_graph.node_ids(),
                    "subgoal_ids": [s.id for s in decomposition.task_graph.subgoals],
                    "rationale": decomposition.rationale[:500],
                },
                actor="decomposer",
                parent_event_id=last_event_id,
            )
            state = state.with_event(produced_event)
            last_event_id = produced_event.event_id

        active = (
            subgoal
            if subgoal is not None
            else (state.task_graph.subgoals[0] if state.task_graph.subgoals else None)
        )
        if active is None:
            return self._fail_no_subgoal(state, last_event_id)
        return await self._loop.run(state, active, actor, monitor, parent_event_id=last_event_id)

    def _fail_no_subgoal(
        self, state: BrainState, parent_event_id: str | None
    ) -> tuple[BrainState, PlanningOutcome]:
        failure = PlanningFailure(
            stage=PlanningStage.PROPOSAL,
            reason="no active subgoal to plan for",
            iterations_used=0,
        )
        failure_event = self._loop.mint_event(
            state,
            BrainEventType.BRAIN_PLANNING_FAILED,
            {
                "stage": PlanningStage.PROPOSAL.value,
                "reason": failure.reason[:500],
                "iterations_used": 0,
            },
            actor="loop",
            parent_event_id=parent_event_id,
        )
        state = state.with_event(failure_event)
        return state, PlanningOutcome(
            status=PlanningStatus.FAILED,
            iterations_used=0,
            accepted_candidate_ids=[],
            monitor_results=[],
            failure=failure,
        )

    def _fail_decomposition(
        self, state: BrainState, reason: str, parent_event_id: str | None
    ) -> tuple[BrainState, PlanningOutcome]:
        failure = PlanningFailure(
            stage=PlanningStage.DECOMPOSITION,
            reason=reason[:MAX_DESCRIPTION],
            iterations_used=0,
        )
        failure_event = self._loop.mint_event(
            state,
            BrainEventType.BRAIN_PLANNING_FAILED,
            {
                "stage": PlanningStage.DECOMPOSITION.value,
                "reason": reason[:500],
                "iterations_used": 0,
            },
            actor="loop",
            parent_event_id=parent_event_id,
        )
        state = state.with_event(failure_event)
        return state, PlanningOutcome(
            status=PlanningStatus.FAILED,
            iterations_used=0,
            accepted_candidate_ids=[],
            monitor_results=[],
            failure=failure,
        )
