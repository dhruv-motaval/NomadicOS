"""Memory + object graph contracts (SPEC §32, §52E-G).

Memory is context only. It must never become authority (§32, §52E).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from nomadicos.contracts.core import Contract


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryProvenance(Contract):
    """Auditable origin (§52G)."""

    source_task_id: str | None = None
    source_event_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    verified: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MemoryRecord(Contract):
    id: str
    kind: MemoryKind
    content: str
    tags: list[str] = Field(default_factory=list)
    provenance: MemoryProvenance
    #: external text stored here remains data; prompt builders wrap/mark it.
    data_only: bool = True

    @model_validator(mode="after")
    def _unverified_never_high_conf(self) -> MemoryRecord:
        if not self.provenance.verified and self.provenance.confidence > 0.95:
            raise ValueError("unverified memory cannot have confidence > 0.95 (§52G)")
        return self


class ObjectRecord(Contract):
    """Entity node of the object memory graph (§52E)."""

    id: str  # e.g. "model:ornith"
    type: str  # person|project|repository|model|tool|application|service|...
    properties: dict[str, Any] = Field(default_factory=dict)


class RelationRecord(Contract):
    id: str
    source: str
    relation: str  # owns|uses|depends_on|runs|strong_for|...
    target: str
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: MemoryProvenance | None = None


class MemoryQuery(Contract):
    text: str
    kinds: list[MemoryKind] = Field(default_factory=list)
    limit: int = Field(default=8, ge=1, le=32)


class RetrievedMemory(Contract):
    record: MemoryRecord | ObjectRecord | RelationRecord
    score: float = Field(ge=0.0, le=1.0)
