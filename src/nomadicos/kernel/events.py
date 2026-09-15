"""Event bus with typed, correlatable events (SPEC §35 auditability).

Events are the audit substrate: a reviewer must be able to reconstruct which
model acted, what it proposed, whether it was authorized, what executed, what
changed, what was verified, and why the task finished.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nomadicos.kernel.ids import new_id

Handler = Callable[["Event", "EventLogger"], None]


class EventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    TASK_STARTED = "TASK_STARTED"
    TASK_CLASSIFIED = "TASK_CLASSIFIED"
    TASK_PLANNED = "TASK_PLANNED"
    REPOSITORY_INSPECTED = "REPOSITORY_INSPECTED"
    GOAL_DEFINED = "GOAL_DEFINED"
    MODEL_SELECTED = "MODEL_SELECTED"
    MODEL_ESCALATED = "MODEL_ESCALATED"
    ACTION_PROPOSED = "ACTION_PROPOSED"
    ACTION_VALIDATED = "ACTION_VALIDATED"
    ACTION_REJECTED = "ACTION_REJECTED"
    AUTHORIZATION_GRANTED = "AUTHORIZATION_GRANTED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    OWNER_CONFLICT_REQUESTED = "OWNER_CONFLICT_REQUESTED"
    OWNER_DECISION = "OWNER_DECISION"
    PERMISSION_GRANTED = "PERMISSION_GRANTED"
    PERMISSION_REVOKED = "PERMISSION_REVOKED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    GOAL_VERIFIED = "GOAL_VERIFIED"
    RECOVERY_STARTED = "RECOVERY_STARTED"
    STUCK_DETECTED = "STUCK_DETECTED"
    CRITIC_REVIEWED = "CRITIC_REVIEWED"
    MEMORY_UPDATED = "MEMORY_UPDATED"
    BENCHMARK_RECORDED = "BENCHMARK_RECORDED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_BLOCKED = "TASK_BLOCKED"


class Event:
    """One correlated audit fact (SPEC §35 correlation fields)."""

    __slots__ = (
        "id",
        "type",
        "task_id",
        "step_id",
        "attempt",
        "model_id",
        "tool",
        "capability",
        "result",
        "payload",
        "timestamp",
    )

    def __init__(
        self,
        type: EventType,
        *,
        task_id: str | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
        model_id: str | None = None,
        tool: str | None = None,
        capability: str | None = None,
        result: str | None = None,
        payload: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        self.id = new_id("evt")
        self.type = type
        self.task_id = task_id
        self.step_id = step_id
        self.attempt = attempt
        self.model_id = model_id
        self.tool = tool
        self.capability = capability
        self.result = result
        self.payload = payload or {}
        self.timestamp = timestamp or datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "task_id": self.task_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "model_id": self.model_id,
            "tool": self.tool,
            "capability": self.capability,
            "result": self.result,
            "payload": self.payload,
            "timestamp": self.timestamp.isoformat(),
        }


class EventBus:
    """Synchronous fan-out bus. Subscriber failures never mask domain results."""

    def __init__(self) -> None:
        self._handlers: dict[EventType | None, list[Handler]] = {}

    def subscribe(self, handler: Handler, *types: EventType) -> None:
        key: EventType | None = types[0] if types else None
        self._handlers.setdefault(key, []).append(handler)

    def publish(self, event: Event, logger: EventLogger) -> None:
        subscribers = list(self._handlers.get(None, []))
        subscribers += list(self._handlers.get(event.type, []))
        failures: list[str] = []
        for handler in subscribers:
            try:
                handler(event, logger)
            except Exception as exc:  # subscriber isolation
                failures.append(f"{handler!r}: {exc}")
        if failures:
            logger.publish(
                Event(
                    EventType.VERIFICATION_RESULT,
                    task_id=event.task_id,
                    result="SUBSCRIBER_ERROR",
                    payload={"failures": failures},
                )
            )


class EventLogger:
    """Append-only sink: forwards every event to an optional durable store
    and to the subscriber bus. The store contract is injected to keep the
    kernel free of persistence imports (SPEC §33: learning/memory/audit never
    sit on the authority path)."""

    def __init__(
        self, bus: EventBus | None = None, sink: Callable[[Event], None] | None = None
    ) -> None:
        self.bus = bus or EventBus()
        self.sink = sink
        self._events: list[Event] = []

    def publish(self, event: Event) -> Event:
        self._events.append(event)
        if self.sink is not None:
            try:
                self.sink(event)
            except Exception:
                pass  # durable audit failure must not crash execution path
        self.bus.publish(event, self)
        return event

    def log(self, type: EventType, **fields: Any) -> Event:
        event = Event(type, **fields)
        return self.publish(event)

    def events(self, task_id: str | None = None) -> list[Event]:
        return [e for e in self._events if task_id is None or e.task_id == task_id]
