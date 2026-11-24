"""Brain trace reconstruction (NomadicBrain spec §1.6, §31, §38; SPEC §35).

Rebuilds a mission's decision trajectory from captured ``BrainEvent``s:
causal order (parent before child), grouped into cognitive cycles with
candidates, monitor results, predictions, evaluations and the selected
action. Read-only and deterministic: the same events always produce the
same trace and the same canonical JSON. A trace is DATA — it never
authorizes anything and never claims task SUCCESS (SPEC §27-29).

Malformed input fails closed (``InvalidTrace``) instead of producing a
plausible-looking but wrong trajectory.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from typing import Any

from pydantic import Field

from nomadicos.brain.errors import InvalidTrace
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.schemas import canonical_json
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract

_FAILURE_TYPES = frozenset(
    {
        BrainEventType.BRAIN_PLANNING_FAILED,
        BrainEventType.BRAIN_DECOMPOSITION_FAILED,
        BrainEventType.BRAIN_TASK_FAILED,
        BrainEventType.BRAIN_TASK_BLOCKED,
    }
)


_ADAPTIVE_TYPES = frozenset(
    {
        BrainEventType.BRAIN_ATTENTION_FOCUSED,
        BrainEventType.BRAIN_BELIEFS_ASSESSED,
        BrainEventType.BRAIN_METACOGNITION_PRODUCED,
        BrainEventType.BRAIN_INFO_GATHERING_REQUESTED,
        BrainEventType.BRAIN_EFFORT_DECIDED,
    }
)


class TraceCycle(Contract):
    """One cognitive cycle (spec §38): propose -> monitor -> deliberate."""

    cycle: int
    subgoal_id: str = ""
    candidate_ids: list[str] = Field(default_factory=list)
    monitor_results: list[dict[str, Any]] = Field(default_factory=list)
    accepted_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    predictions: list[dict[str, Any]] = Field(default_factory=list)
    evaluations: list[dict[str, Any]] = Field(default_factory=list)
    selected_action: dict[str, Any] | None = None
    #: adaptive pass before proposing (attention/beliefs/metacognition/
    #: information/effort), keyed by event type
    adaptive: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: measured prediction vs reality for the executed selection
    prediction_errors: list[dict[str, Any]] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


class BrainTrace(Contract):
    """A mission's reconstructed, replayable decision trajectory."""

    mission_id: str
    #: events in causal order
    events: list[BrainEvent]
    #: events before the first cycle (decomposition, attention, effort, ...)
    preamble_event_ids: list[str] = Field(default_factory=list)
    cycles: list[TraceCycle] = Field(default_factory=list)
    #: payloads of failure/blocked events, in order
    failures: list[dict[str, Any]] = Field(default_factory=list)
    #: parent ids referenced but not captured (trace starts mid-mission)
    external_parent_ids: list[str] = Field(default_factory=list)

    @property
    def selected_action(self) -> dict[str, Any] | None:
        """The last selected action across cycles, if any."""
        for cycle in reversed(self.cycles):
            if cycle.selected_action is not None:
                return cycle.selected_action
        return None

    def to_canonical_json(self) -> str:
        return canonical_json(self)


def reconstruct_trace(
    events: Sequence[BrainEvent], *, state: BrainState | None = None
) -> BrainTrace:
    """Reconstruct the trajectory of one mission.

    Without ``state`` the order is a causal topological sort (ties broken
    by timestamp, then event id, so input order never matters). With
    ``state`` its ``history`` is the authoritative order and must reference
    exactly the given events.
    """
    if not events:
        raise InvalidTrace("no events to reconstruct")
    missions = {e.mission_id for e in events}
    if len(missions) != 1:
        raise InvalidTrace("events span multiple missions", missions=sorted(missions))
    mission_id = next(iter(missions))
    by_id: dict[str, BrainEvent] = {}
    for event in events:
        if event.event_id in by_id:
            raise InvalidTrace("duplicate event id", event_id=event.event_id)
        by_id[event.event_id] = event

    ordered = _history_order(by_id, state) if state is not None else _causal_order(events)
    position = {e.event_id: i for i, e in enumerate(ordered)}
    external: list[str] = []
    for event in ordered:
        parent = event.parent_event_id
        if parent is None:
            continue
        if parent not in position:
            if parent not in external:
                external.append(parent)
        elif position[parent] >= position[event.event_id]:
            raise InvalidTrace(
                "event precedes its causal parent", event_id=event.event_id, parent=parent
            )

    preamble, cycles, failures = _group(ordered)
    return BrainTrace(
        mission_id=mission_id,
        events=ordered,
        preamble_event_ids=preamble,
        cycles=cycles,
        failures=failures,
        external_parent_ids=external,
    )


def _history_order(by_id: dict[str, BrainEvent], state: BrainState) -> list[BrainEvent]:
    mission_id = next(iter(by_id.values())).mission_id
    if state.mission_id != mission_id:
        raise InvalidTrace("state belongs to a different mission", state_mission=state.mission_id)
    history = [ref.event_id for ref in state.history]
    if len(set(history)) != len(history):
        raise InvalidTrace("state history repeats an event id")
    if set(history) != set(by_id):
        raise InvalidTrace(
            "events do not match state history",
            missing=sorted(set(history) - set(by_id)),
            unrecorded=sorted(set(by_id) - set(history)),
        )
    return [by_id[event_id] for event_id in history]


def _causal_order(events: Sequence[BrainEvent]) -> list[BrainEvent]:
    """Kahn topological sort; parents outside the capture count as roots."""
    ids = {e.event_id for e in events}
    children: dict[str, list[int]] = {}
    pending: list[int] = [0] * len(events)
    for index, event in enumerate(events):
        parent = event.parent_event_id
        if parent is not None and parent in ids:
            children.setdefault(parent, []).append(index)
            pending[index] = 1
    ready = [(e.timestamp, e.event_id, i) for i, e in enumerate(events) if pending[i] == 0]
    heapq.heapify(ready)
    ordered: list[BrainEvent] = []
    while ready:
        _, _, index = heapq.heappop(ready)
        event = events[index]
        ordered.append(event)
        for child in children.get(event.event_id, []):
            pending[child] -= 1
            if pending[child] == 0:
                heapq.heappush(ready, (events[child].timestamp, events[child].event_id, child))
    if len(ordered) != len(events):
        raise InvalidTrace("causal cycle in event parents")
    return ordered


def _group(
    ordered: list[BrainEvent],
) -> tuple[list[str], list[TraceCycle], list[dict[str, Any]]]:
    preamble: list[str] = []
    cycles: list[TraceCycle] = []
    failures: list[dict[str, Any]] = []
    adaptive_open = False  # current cycle was opened by an adaptive pass
    for event in ordered:
        kind, payload = event.event_type, event.payload
        if kind is BrainEventType.BRAIN_ATTENTION_FOCUSED:
            starts_cycle = True
        elif kind is BrainEventType.BRAIN_CANDIDATES_GENERATED:
            # the adaptive pass and the proposals that follow are one cycle
            starts_cycle = not (adaptive_open and cycles and not cycles[-1].candidate_ids)
        else:
            starts_cycle = kind is BrainEventType.BRAIN_SEARCH_STARTED and not cycles
        if starts_cycle:
            adaptive_open = kind is BrainEventType.BRAIN_ATTENTION_FOCUSED
            cycles.append(
                TraceCycle(cycle=len(cycles) + 1, subgoal_id=str(payload.get("subgoal_id", "")))
            )
        if kind in _FAILURE_TYPES:
            failures.append({"event_type": kind.value, **payload})
        if not cycles:
            preamble.append(event.event_id)
            continue
        current = cycles[-1]
        current.event_ids.append(event.event_id)
        if kind is BrainEventType.BRAIN_CANDIDATES_GENERATED:
            current.candidate_ids.extend(payload.get("candidate_ids", []))
        elif kind is BrainEventType.BRAIN_MONITOR_EVALUATED:
            current.monitor_results.extend(payload.get("results", []))
        elif kind is BrainEventType.BRAIN_CANDIDATE_ACCEPTED:
            current.accepted_candidate_ids.append(str(payload.get("candidate_id", "")))
        elif kind is BrainEventType.BRAIN_CANDIDATE_REJECTED:
            current.rejected_candidate_ids.append(str(payload.get("candidate_id", "")))
        elif kind is BrainEventType.BRAIN_PREDICTION_PRODUCED:
            current.predictions.append(dict(payload))
        elif kind is BrainEventType.BRAIN_EVALUATION_PRODUCED:
            current.evaluations.append(dict(payload))
        elif kind is BrainEventType.BRAIN_ACTION_SELECTED:
            current.selected_action = dict(payload)
        elif kind in _ADAPTIVE_TYPES:
            current.adaptive[kind.value] = dict(payload)
        elif kind is BrainEventType.BRAIN_PREDICTION_ERROR_MEASURED:
            current.prediction_errors.append(dict(payload))
    return preamble, cycles, failures
