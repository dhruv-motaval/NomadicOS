"""Trace correlation + typed in-process event bus (BP §254-255).

Every operation carries request_id → task_id → run_id → step_id so audit,
experience recording, and the future UI stay consistent. The bus is the
cross-cutting spine; publishers must pre-redact payloads (BP §256).
"""

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


def new_id() -> str:
    """Collision-resistant identifier (BP §253)."""
    return str(uuid4())


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Correlation identity for every operation (BP §254, §399, ADR-0026/F5)."""

    request_id: str = field(default_factory=new_id)
    user_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None

    def child(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> "TraceContext":
        """Derive a deeper context; identity fields flow down, never up."""
        return replace(
            self,
            task_id=task_id or self.task_id,
            run_id=run_id or self.run_id,
            step_id=step_id,
        )


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    context: TraceContext
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    """Minimal async pub/sub. Handler failures are logged, never propagated —
    observability must not break the execution path it observes."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        for handler in list(self._handlers.get(event.type, ())):
            try:
                await handler(event)
            except Exception:  # noqa: BLE001 — observability must not break execution
                logger.warning(
                    "event handler failed event_type=%s handler=%s",
                    event.type,
                    getattr(handler, "__qualname__", "?"),
                )

    async def emit(
        self, event_type: str, context: TraceContext, payload: dict[str, Any] | None = None
    ) -> Event:
        event = Event(type=event_type, context=context, payload=payload or {})
        await self.publish(event)
        return event
