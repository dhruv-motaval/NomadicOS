"""AuditSink contract (BP §41-42): append-only, never model-controlled.

Event types cover BP §42 categories + §123 security events. Sinks are
append-only by contract; nothing here can update or delete.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class AuditEventCategory(StrEnum):
    PROCESS_START = "PROCESS_START"
    PROCESS_STOP = "PROCESS_STOP"
    SCREEN_CAPTURE = "SCREEN_CAPTURE"
    MEMORY_READ = "MEMORY_READ"
    MEMORY_WRITE = "MEMORY_WRITE"
    POLICY_CHANGED = "POLICY_CHANGED"
    SECURITY_EVENT = "SECURITY_EVENT"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_DECISION = "TOOL_DECISION"
    TOOL_EXECUTED = "TOOL_EXECUTED"
    MODEL_SELECTED = "MODEL_SELECTED"
    MODEL_CALL = "MODEL_CALL"
    NETWORK_REQUEST = "NETWORK_REQUEST"
    TASK_EVENT = "TASK_EVENT"
    IMPROVEMENT_PROPOSED = "IMPROVEMENT_PROPOSED"
    IMPROVEMENT_PROMOTED = "IMPROVEMENT_PROMOTED"
    IMPROVEMENT_REJECTED = "IMPROVEMENT_REJECTED"
    ROLLBACK = "ROLLBACK"


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    category: AuditEventCategory
    severity: AuditSeverity = AuditSeverity.INFO
    timestamp: datetime = Field(default_factory=datetime.now)
    user_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    subject: str | None = None  # tool name / destination / model id
    decision: str | None = None  # ALLOW / DENY / ASK / BLOCK ...
    reason: str | None = Field(default=None, max_length=1024)
    fields: dict[str, Any] = Field(default_factory=dict)


def audit_event_types() -> list[str]:
    return [c.value for c in AuditEventCategory]


class AuditSink(ABC):
    """Append-only audit target. Implementations must never expose update/delete."""

    @abstractmethod
    async def append(self, event: AuditEvent) -> None: ...

    @abstractmethod
    async def query(
        self,
        *,
        category: AuditEventCategory | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Read-only inspection (BP §281: no black-box execution)."""


class InMemoryAuditSink(AuditSink):
    """Simple in-memory sink; the PostgreSQL sink replaces it in Phase 3."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    async def query(
        self,
        *,
        category: AuditEventCategory | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        selected = [
            e
            for e in self.events
            if (category is None or e.category is category)
            and (task_id is None or e.task_id == task_id)
        ]
        return selected[-limit:]
