"""Brain trace reconstruction (NomadicBrain spec §1.6, §31, §38)."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from itertools import count

import pytest

from nomadicos.brain.deliberation import DeliberationCoordinator
from nomadicos.brain.errors import InvalidTrace
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.loop import ActorMonitorLoop
from nomadicos.brain.state import BrainState
from nomadicos.brain.trace import reconstruct_trace

from .test_deliberation import (
    AcceptingMonitor,
    ScriptedActor,
    ScriptedEvaluator,
    ScriptedPredictor,
    active_subgoal,
    make_candidate,
    make_state,
)

FIXED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


async def _run_mission() -> tuple[BrainState, list[BrainEvent]]:
    """Real MAP loop + deliberation run with deterministic ids/clock."""
    ids = count(1)
    events: list[BrainEvent] = []
    clock = {"now": lambda: FIXED_AT, "event_id": lambda: f"evt_{next(ids):03d}"}
    state = make_state()
    actor = ScriptedActor([[make_candidate("c1"), make_candidate("c2")]])
    monitor = AcceptingMonitor()
    loop = ActorMonitorLoop(events=events, **clock)  # type: ignore[arg-type]
    state, outcome = await loop.run(state, active_subgoal(state), actor, monitor)
    coordinator = DeliberationCoordinator(
        actor,
        monitor,
        ScriptedPredictor([0.8, 0.9]),
        ScriptedEvaluator([0.65, 0.88]),
        events=events,
        **clock,  # type: ignore[arg-type]
    )
    state, delib = await coordinator.deliberate(
        state, active_subgoal(state), outcome, parent_event_id=state.history[-1].event_id
    )
    assert delib.decision is not None
    return state, events


@pytest.mark.asyncio
async def test_reconstructs_full_mission_cycle() -> None:
    state, events = await _run_mission()
    trace = reconstruct_trace(events, state=state)

    assert trace.mission_id == state.mission_id
    assert [e.event_id for e in trace.events] == [r.event_id for r in state.history]
    assert trace.preamble_event_ids == []
    assert trace.external_parent_ids == []
    assert trace.failures == []
    assert len(trace.cycles) == 1
    cycle = trace.cycles[0]
    assert cycle.subgoal_id == "sub_s1"
    assert cycle.candidate_ids == ["c1", "c2"]
    assert cycle.accepted_candidate_ids == ["c1", "c2"]
    assert {r["candidate_id"] for r in cycle.monitor_results} == {"c1", "c2"}
    assert {p["candidate_id"] for p in cycle.predictions} == {"c1", "c2"}
    assert len(cycle.evaluations) == len(cycle.predictions)
    assert trace.selected_action is not None
    assert trace.selected_action["candidate_id"] == "c2"
    assert cycle.event_ids == [e.event_id for e in trace.events]


@pytest.mark.asyncio
async def test_causal_order_recovered_from_shuffled_events() -> None:
    state, events = await _run_mission()
    expected = reconstruct_trace(events, state=state)
    shuffled = list(events)
    random.Random(7).shuffle(shuffled)

    trace = reconstruct_trace(shuffled)

    assert [e.event_id for e in trace.events] == [e.event_id for e in expected.events]
    assert trace.to_canonical_json() == expected.to_canonical_json()


@pytest.mark.asyncio
async def test_partial_capture_records_external_parent() -> None:
    _, events = await _run_mission()
    trace = reconstruct_trace(events[3:])
    assert trace.external_parent_ids == [events[2].event_id]


def _evt(event_id: str, parent: str | None = None, **kw: object) -> BrainEvent:
    fields: dict[str, object] = {
        "event_id": event_id,
        "mission_id": "mission_t",
        "timestamp": FIXED_AT,
        "event_type": BrainEventType.BRAIN_TASK_STARTED,
        "parent_event_id": parent,
    }
    fields.update(kw)
    return BrainEvent(**fields)  # type: ignore[arg-type]


def test_preamble_and_failures_are_kept() -> None:
    events = [
        _evt("e1", event_type=BrainEventType.BRAIN_DECOMPOSITION_REQUESTED),
        _evt(
            "e2",
            "e1",
            event_type=BrainEventType.BRAIN_PLANNING_FAILED,
            payload={"reason": "no subgoals"},
        ),
    ]
    trace = reconstruct_trace(events)
    assert trace.preamble_event_ids == ["e1", "e2"]
    assert trace.cycles == []
    assert trace.selected_action is None
    assert trace.failures == [{"event_type": "BRAIN_PLANNING_FAILED", "reason": "no subgoals"}]


@pytest.mark.parametrize(
    ("events", "match"),
    [
        ([], "no events"),
        ([_evt("e1"), _evt("e2", mission_id="other")], "multiple missions"),
        ([_evt("e1"), _evt("e1")], "duplicate event id"),
        ([_evt("e1", "e2"), _evt("e2", "e1")], "causal cycle"),
    ],
)
def test_malformed_trajectories_fail_closed(events: list[BrainEvent], match: str) -> None:
    with pytest.raises(InvalidTrace, match=match):
        reconstruct_trace(events)


@pytest.mark.asyncio
async def test_state_history_mismatch_fails_closed() -> None:
    state, events = await _run_mission()
    with pytest.raises(InvalidTrace, match="do not match state history"):
        reconstruct_trace(events[:-1], state=state)
    foreign = state.model_copy(update={"mission_id": "mission_other"})
    with pytest.raises(InvalidTrace, match="different mission"):
        reconstruct_trace(events, state=foreign)


def test_history_order_that_breaks_causality_fails_closed() -> None:
    events = [_evt("e1", mission_id="mission_x"), _evt("e2", "e1", mission_id="mission_x")]
    state = make_state().model_copy(update={"mission_id": "mission_x"})
    for event in reversed(events):
        state = state.with_event(event)
    with pytest.raises(InvalidTrace, match="precedes its causal parent"):
        reconstruct_trace(events, state=state)
