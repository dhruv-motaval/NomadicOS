"""Runtime lifecycle: startup/shutdown flows (BP §234-235) and task state machine
(BP §137). Startup initializes security before autonomous execution; shutdown is
graceful; emergency stop is out-of-band (ADR-0016).
"""

import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from nomadicos.core.events import EventBus

logger = logging.getLogger(__name__)


class TaskStatus(StrEnum):
    """Explicit task state machine (BP §137). Free-form agent text never drives
    state. This is the ONE authoritative lifecycle vocabulary (STEP 4):
    runtime transitions -> PostgreSQL mirror — no second mutable store.

    Name mapping for the qualification vocabulary: CREATED is the persisted
    creation state; COMPLETED maps to SUCCESS, RECOVERY maps to RECOVERING
    (kept for API/report compatibility)."""

    CREATED = "CREATED"
    PLANNED = "PLANNED"
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"  # awaiting owner authorization (ASK w/o approver)
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"  # BP §181


# Legal transitions (BP §137/§52/§121/§135). Anything else is a bug, not a state.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.PLANNED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PLANNED: frozenset(
        {
            TaskStatus.AUTHORIZED,
            TaskStatus.RUNNING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.AUTHORIZED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.WAITING,
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.BLOCKED,
            TaskStatus.SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.RECOVERING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.RECOVERING,
            TaskStatus.SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.PARTIALLY_COMPLETED,
        }
    ),
    TaskStatus.RECOVERING: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.VERIFYING,
            TaskStatus.FAILED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.RUNNING, TaskStatus.RECOVERING, TaskStatus.CANCELLED}
    ),
    TaskStatus.SUCCESS: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.RECOVERING}),
    TaskStatus.PARTIALLY_COMPLETED: frozenset({TaskStatus.RECOVERING}),
    TaskStatus.CANCELLED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCESS,
        TaskStatus.FAILED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.CANCELLED,
    }
)

# Crash-reconcilable states: a row in any of these after a restart was left
# unfinished by the dead process — it must never be reported as successful.
# BLOCKED is EXCLUDED deliberately: waiting on the owner is a durable, chosen
# outcome, not an interrupted one.
INTERRUPT_STATUSES = frozenset(
    {TaskStatus.CREATED, TaskStatus.PLANNED, TaskStatus.AUTHORIZED}
    | {TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.VERIFYING}
    | {TaskStatus.RECOVERING}
)


class StateTransitionError(Exception):
    pass


class TaskState(BaseModel):
    """Versioned task state persisted for safe resumption (BP §136, §391)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskStatus = TaskStatus.PLANNED
    plan_version: int = 0
    completed_steps: list[str] = []
    pending_steps: list[str] = []
    last_verified_state: dict[str, Any] = {}
    attempts: int = 0

    def transition(self, to: TaskStatus) -> TaskStatus:
        allowed = TASK_TRANSITIONS[self.status]
        if to not in allowed:
            raise StateTransitionError(
                f"illegal transition {self.status.value} -> {to.value} (task {self.task_id})"
            )
        self.status = to
        return self.status


class LifecycleEvent(StrEnum):
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class Lifecycle:
    """Owns the startup (§234) / shutdown (§235) sequences and their events.

    Subsystem initializers run in registration order; shutdown runs in reverse.
    The emergency stop is a separate latch: once pulled, no new task may enter
    RUNNING until explicitly reset — and the model can never reach this class.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._initializers: list[tuple[str, Any]] = []
        self._shutdowns: list[tuple[str, Any]] = []
        self._started: list[str] = []
        self.state: str = "CREATED"
        self._emergency_stopped = False

    def register(self, name: str, initializer, shutdown=None) -> None:
        self._initializers.append((name, initializer))
        if shutdown is not None:
            self._shutdowns.append((name, shutdown))

    @property
    def emergency_stopped(self) -> bool:
        return self._emergency_stopped

    async def startup(self) -> None:
        self.state = "STARTING"
        await self._bus.emit(LifecycleEvent.STARTING.value, _lifecycle_ctx(), {})
        for name, initializer in self._initializers:
            await initializer()
            self._started.append(name)
            logger.info("lifecycle subsystem started subsystem=%s", name)
        self.state = "READY"
        await self._bus.emit(LifecycleEvent.READY.value, _lifecycle_ctx(), {})

    async def shutdown(self) -> None:
        self.state = "STOPPING"
        for name, shutdown in reversed(self._shutdowns):
            if name in self._started:
                try:
                    await shutdown()
                except Exception:  # noqa: BLE001 — best-effort graceful stop (§235)
                    logger.warning("lifecycle shutdown failed subsystem=%s", name)
        self.state = "STOPPED"
        await self._bus.emit(LifecycleEvent.STOPPED.value, _lifecycle_ctx(), {})

    def emergency_stop(self) -> None:
        """Out-of-band halt (BP §122): latch + event, no model involvement."""
        self._emergency_stopped = True
        self.state = "EMERGENCY_STOP"
        import asyncio

        asyncio.get_running_loop().create_task(
            self._bus.emit(LifecycleEvent.EMERGENCY_STOP.value, _lifecycle_ctx(), {})
        )

    def reset_emergency_stop(self) -> None:
        """Owner action (CLI), never a model action."""
        self._emergency_stopped = False


def _lifecycle_ctx():
    from nomadicos.core.events import TraceContext

    return TraceContext(request_id="lifecycle", user_id="system")
