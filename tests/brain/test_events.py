"""Brain events: identity, determinism, linkage, schema version (Sprint 1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nomadicos.brain.events import BRAIN_EVENT_SCHEMA_VERSION, BrainEvent, BrainEventType, EventRef

FIXED_AT = datetime(2026, 9, 28, 1, 2, 3, 456789, tzinfo=UTC)


def _event(**overrides: object) -> BrainEvent:
    fields: dict[str, object] = {
        "event_id": "evt_fixed",
        "mission_id": "mission_test",
        "timestamp": FIXED_AT,
        "event_type": BrainEventType.BRAIN_TASK_STARTED,
    }
    fields.update(overrides)
    return BrainEvent(**fields)  # type: ignore[arg-type]


def test_event_minimal_construction_and_defaults() -> None:
    event = _event()
    assert event.event_id == "evt_fixed"
    assert event.actor == "orchestrator"
    assert event.parent_event_id is None
    assert event.payload == {}
    assert event.schema_version == BRAIN_EVENT_SCHEMA_VERSION


def test_event_type_vocabulary_is_closed() -> None:
    assert {t.value for t in BrainEventType} == {
        "BRAIN_STATE_CREATED",
        "BRAIN_TASK_STARTED",
        "BRAIN_TASK_COMPLETED",
        "BRAIN_TASK_FAILED",
        "BRAIN_TASK_BLOCKED",
        "BRAIN_GRAPH_COMPLETED",
        # Sprint 2 — MAP core planning trajectory
        "BRAIN_DECOMPOSITION_REQUESTED",
        "BRAIN_DECOMPOSITION_PRODUCED",
        "BRAIN_DECOMPOSITION_FAILED",
        "BRAIN_CANDIDATES_GENERATED",
        "BRAIN_MONITOR_EVALUATED",
        "BRAIN_CANDIDATE_ACCEPTED",
        "BRAIN_CANDIDATE_REJECTED",
        "BRAIN_ACTOR_REFINEMENT",
        "BRAIN_PLANNING_FAILED",
    }


def test_default_ids_are_prefixed_and_unique() -> None:
    first = BrainEvent(mission_id="mission_test", event_type=BrainEventType.BRAIN_STATE_CREATED)
    second = BrainEvent(mission_id="mission_test", event_type=BrainEventType.BRAIN_STATE_CREATED)
    assert first.event_id.startswith("evt_")
    assert first.event_id != second.event_id


def test_event_deterministic_serialization_and_round_trip() -> None:
    event = _event(payload={"task": "a"})
    assert event.to_canonical_json() == event.to_canonical_json()
    restored = BrainEvent.model_validate_json(event.model_dump_json())
    assert restored == event
    assert restored.to_canonical_json() == event.to_canonical_json()


def test_two_identical_events_serialize_identically() -> None:
    assert _event().to_canonical_json() == _event().to_canonical_json()
    parent = _event(event_type=BrainEventType.BRAIN_TASK_COMPLETED, parent_event_id="evt_fixed")
    assert parent.parent_event_id == "evt_fixed"


def test_parent_and_mission_linkage() -> None:
    linked = _event(
        event_type=BrainEventType.BRAIN_TASK_COMPLETED,
        parent_event_id="evt_parent",
        mission_id="mission_other",
    )
    assert linked.parent_event_id == "evt_parent"
    assert linked.mission_id == "mission_other"
    assert BrainEvent.model_validate_json(linked.model_dump_json()).parent_event_id == "evt_parent"


def test_mission_id_required() -> None:
    with pytest.raises(ValidationError, match="mission_id"):
        BrainEvent(
            mission_id="  ",
            event_type=BrainEventType.BRAIN_STATE_CREATED,
            timestamp=FIXED_AT,
        )


def test_schema_version_fails_closed() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        _event(schema_version=99)


def test_event_rejects_authority_shaped_extras() -> None:
    with pytest.raises(ValidationError):
        _event(authorized=True)  # type: ignore[arg-type]


def test_payload_is_bounded() -> None:
    with pytest.raises(ValidationError, match="payload"):
        _event(payload={f"k{i}": i for i in range(33)})


def test_actor_must_be_nonempty() -> None:
    with pytest.raises(ValidationError, match="actor"):
        _event(actor="  ")


def test_event_ref_requires_nonempty_id() -> None:
    assert EventRef(event_id="evt_x").event_id == "evt_x"
    with pytest.raises(ValidationError, match="event_id"):
        EventRef(event_id="  ")
    assert EventRef.model_validate_json(EventRef(event_id="evt_x").model_dump_json()) == EventRef(
        event_id="evt_x"
    )
