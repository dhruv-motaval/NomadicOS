"""Brain events — deterministic, serializable, mission-correlated
(NomadicBrain spec §6).

Phase 1 defines the event DATA model only: no event bus, no dispatch, no
side effects. The kernel audit bus (``nomadicos.kernel.events``) remains
the system-wide audit substrate; brain events are mission-trajectory
records with a different correlation shape (mission, causal parent,
schema version), so a later sprint may bridge them into the kernel
logger rather than the brain maintaining a second bus.

Every event carries: ``event_id``, ``mission_id``, ``timestamp``,
``event_type``, ``actor``, ``parent_event_id`` (causal reference),
``schema_version``, ``payload``. Determinism: identity is explicit and
stable, serialization is canonical, and timestamps are injectable so
replayed trajectories serialize identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.schemas import canonical_json
from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id

#: event schema version; mismatches fail closed
BRAIN_EVENT_SCHEMA_VERSION = 1

MAX_PAYLOAD_KEYS = 32


class BrainEventType(StrEnum):
    """Mission-trajectory event types (Sprint 1 + Sprint 2 MAP core)."""

    BRAIN_STATE_CREATED = "BRAIN_STATE_CREATED"
    BRAIN_TASK_STARTED = "BRAIN_TASK_STARTED"
    BRAIN_TASK_COMPLETED = "BRAIN_TASK_COMPLETED"
    BRAIN_TASK_FAILED = "BRAIN_TASK_FAILED"
    BRAIN_TASK_BLOCKED = "BRAIN_TASK_BLOCKED"
    BRAIN_GRAPH_COMPLETED = "BRAIN_GRAPH_COMPLETED"
    # Sprint 2 — MAP core planning trajectory
    BRAIN_DECOMPOSITION_REQUESTED = "BRAIN_DECOMPOSITION_REQUESTED"
    BRAIN_DECOMPOSITION_PRODUCED = "BRAIN_DECOMPOSITION_PRODUCED"
    BRAIN_DECOMPOSITION_FAILED = "BRAIN_DECOMPOSITION_FAILED"
    BRAIN_CANDIDATES_GENERATED = "BRAIN_CANDIDATES_GENERATED"
    BRAIN_MONITOR_EVALUATED = "BRAIN_MONITOR_EVALUATED"
    BRAIN_CANDIDATE_ACCEPTED = "BRAIN_CANDIDATE_ACCEPTED"
    BRAIN_CANDIDATE_REJECTED = "BRAIN_CANDIDATE_REJECTED"
    BRAIN_ACTOR_REFINEMENT = "BRAIN_ACTOR_REFINEMENT"
    BRAIN_PLANNING_FAILED = "BRAIN_PLANNING_FAILED"


class BrainEvent(Contract):
    """One traceable brain fact, correlated to a mission.

    An event is DATA: it records what happened and never carries or
    grants authority. Closed schema: unknown fields (including
    authority-shaped ones) are rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    mission_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: BrainEventType
    #: who/what produced the event (e.g. "orchestrator", "decomposer")
    actor: str = "orchestrator"
    #: causal reference to the event this one follows, if any
    parent_event_id: str | None = None
    schema_version: int = BRAIN_EVENT_SCHEMA_VERSION
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> BrainEvent:
        if not self.mission_id.strip():
            raise ValueError("event mission_id must not be empty")
        if not self.actor.strip():
            raise ValueError("event actor must not be empty")
        if self.schema_version != BRAIN_EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported brain event schema_version {self.schema_version}; "
                f"expected {BRAIN_EVENT_SCHEMA_VERSION}"
            )
        if len(self.payload) > MAX_PAYLOAD_KEYS:
            raise ValueError(f"event payload exceeds {MAX_PAYLOAD_KEYS} keys")
        return self

    def to_canonical_json(self) -> str:
        """Deterministic canonical serialization."""
        return canonical_json(self)


class EventRef(Contract):
    """Minimal reference from ``BrainState.history`` to a brain event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str

    @model_validator(mode="after")
    def _id_nonempty(self) -> EventRef:
        if not self.event_id.strip():
            raise ValueError("event reference event_id must not be empty")
        return self
