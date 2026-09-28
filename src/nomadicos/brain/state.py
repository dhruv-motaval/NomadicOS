"""Canonical BrainState (NomadicBrain spec §5-6, Sprint 1).

The single cognitive state object: typed, frozen, deterministic,
serializable, and versioned. It contains DATA only — no model clients,
no prompts, no inference calls, no tool execution, no orchestration
side effects. Transitions produce NEW instances (the event/state reducer
model of spec §6), never in-place mutation.

Fields reuse existing NomadicOS contracts wherever one exists (see
``nomadicos.brain.schemas`` for the reuse map). ``task_graph.goal_id``
must match ``goal.id``; the active subgoal must belong to the graph.

No wall-clock defaults live here: the same inputs construct equal
states, so trajectories replay deterministically. Temporal facts are
carried by events (``nomadicos.brain.events``), whose ids/timestamps
are minted by the caller and referenced from ``history``.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.errors import InvalidBrainState
from nomadicos.brain.events import BrainEvent, EventRef
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.schemas import (
    BRAIN_SCHEMA_VERSION,
    MAX_MONITOR_RESULTS,
    MAX_PLAN_CANDIDATES,
    MAX_PROVENANCE_REFS,
    ActionCandidate,
    BudgetState,
    Capability,
    Constraints,
    Decision,
    Hypothesis,
    MemoryRef,
    MonitorResult,
    Subgoal,
    WorkingMemoryView,
    WorldState,
    canonical_json,
)
from nomadicos.contracts.core import Contract, Goal
from nomadicos.contracts.execution import Observation
from nomadicos.contracts.verification import EvidenceItem
from nomadicos.kernel.ids import new_id

#: BrainState schema version; mismatches fail closed on load
BRAIN_STATE_SCHEMA_VERSION = BRAIN_SCHEMA_VERSION


class BrainState(Contract):
    """One mission's cognitive state (spec §5).

    Immutable value object: ``model_copy``-based transitions only. No
    authority, no execution, no verification semantics — those remain
    in the existing NomadicOS boundaries.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str = Field(default_factory=lambda: new_id("mission"))
    schema_version: int = BRAIN_STATE_SCHEMA_VERSION
    #: deterministic instance version; every transition bumps it
    state_version: int = Field(default=1, ge=1)
    goal: Goal
    world: WorldState = Field(default_factory=WorldState)
    task_graph: TaskGraph
    active_subgoal: Subgoal | None = None
    working_memory: WorkingMemoryView = Field(default_factory=WorkingMemoryView)
    relevant_memories: list[MemoryRef] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    budget: BudgetState = Field(default_factory=BudgetState)
    history: list[EventRef] = Field(default_factory=list)
    # Sprint 2 — MAP planning state (data only; see brain.map modules)
    action_candidates: list[ActionCandidate] = Field(default_factory=list)
    monitor_results: list[MonitorResult] = Field(default_factory=list)
    planning_decisions: list[Decision] = Field(default_factory=list)
    #: bounded refinement iterations consumed so far (0 = none yet)
    planning_iterations: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _coherent(self) -> BrainState:
        if not self.mission_id.strip():
            raise ValueError("brain state mission_id must not be empty")
        if self.schema_version != BRAIN_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported brain state schema_version {self.schema_version}; "
                f"expected {BRAIN_STATE_SCHEMA_VERSION}"
            )
        if self.task_graph.goal_id != self.goal.id:
            raise ValueError(
                f"task graph belongs to goal {self.task_graph.goal_id!r}, "
                f"not to the state's goal {self.goal.id!r}"
            )
        if self.active_subgoal is not None:
            if self.active_subgoal.goal_id != self.goal.id:
                raise ValueError("active subgoal does not belong to the state's goal")
            if self.active_subgoal.id not in {s.id for s in self.task_graph.subgoals}:
                raise ValueError("active subgoal is not part of the state's task graph")
        if len(self.action_candidates) > MAX_PLAN_CANDIDATES:
            raise ValueError(f"action candidates exceed {MAX_PLAN_CANDIDATES}")
        if len(self.monitor_results) > MAX_MONITOR_RESULTS:
            raise ValueError(f"monitor results exceed {MAX_MONITOR_RESULTS}")
        if len(self.planning_decisions) > MAX_PLAN_CANDIDATES:
            raise ValueError(f"planning decisions exceed {MAX_PLAN_CANDIDATES}")
        for candidate in self.action_candidates:
            if len(candidate.derived_from) > MAX_PROVENANCE_REFS:
                raise ValueError("stored candidate provenance exceeds the reference bound")
        return self

    @classmethod
    def for_goal(
        cls,
        goal: Goal,
        *,
        task_graph: TaskGraph | None = None,
        mission_id: str | None = None,
    ) -> BrainState:
        """Construct the initial state for a goal. Without a graph an empty
        graph (not yet decomposed) is used; an empty graph is never
        complete, so no false completion can arise."""
        graph = task_graph if task_graph is not None else TaskGraph.build(goal_id=goal.id)
        if mission_id is None:
            return cls(goal=goal, task_graph=graph)
        return cls(goal=goal, task_graph=graph, mission_id=mission_id)

    def to_canonical_json(self) -> str:
        """Deterministic canonical serialization."""
        return canonical_json(self)

    def with_event(self, event: BrainEvent) -> BrainState:
        """Minimal event→state reducer (spec §6): append the event's
        reference to history. Deterministic — no ids or timestamps are
        minted here; the event is caller-authored and carries its own
        identity. Mission mismatch fails closed."""
        if event.mission_id != self.mission_id:
            raise InvalidBrainState(
                "event does not belong to this mission",
                event_mission_id=event.mission_id,
                state_mission_id=self.mission_id,
            )
        history = [*self.history, EventRef(event_id=event.event_id)]
        return self.model_copy(update={"history": history, "state_version": self.state_version + 1})
