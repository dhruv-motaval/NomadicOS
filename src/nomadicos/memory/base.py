"""MemoryStore interface (BP §94, §112-113, §165-167, §209).

User memory vs system experience are distinct stores (BP §112); this interface
covers user/project memory. Scoping is mandatory (BP §60, §113, §382) — a model
cannot "retrieve everything".
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

Content = str
MemoryID = UUID


class MemoryScope(StrEnum):
    """Memory scopes (BP §113, §382)."""

    SESSION = "session"
    TASK = "task"
    PROJECT = "project"
    USER = "user"
    SYSTEM = "system"


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: MemoryID = Field(default_factory=uuid4)
    scope: MemoryScope
    content: Content = Field(min_length=1, max_length=100_000)
    created_at: datetime = Field(default_factory=datetime.now)
    source: str = Field(min_length=1, max_length=256)  # provenance (BP §89, §166)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified: bool = False
    tags: list[str] = Field(default_factory=list)
    session_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    sensitivity: Literal["public", "internal", "sensitive"] = "internal"

    def matches_scope(
        self,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
    ) -> bool:
        """Scope filter (BP §381): project memories are not auto-exposed cross-project."""
        if self.project_id and project_id and self.project_id != project_id:
            return False
        return True


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10_000)
    scope: MemoryScope | None = None
    session_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    limit: int = Field(default=5, ge=1, le=100)


class MemoryStore(ABC):
    """BP §94 conceptual interface: store/search/get/delete/forget/lock/export."""

    @abstractmethod
    async def store(self, record: MemoryRecord) -> MemoryID: ...

    @abstractmethod
    async def search(self, query: MemoryQuery) -> list[MemoryRecord]: ...

    @abstractmethod
    async def get(self, memory_id: MemoryID) -> MemoryRecord | None: ...

    @abstractmethod
    async def delete(self, memory_id: MemoryID) -> bool: ...

    @abstractmethod
    async def forget(
        self,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """Bulk deletion within scope (BP §59, §402-403); returns deleted count."""

    @abstractmethod
    async def count(self) -> int: ...
