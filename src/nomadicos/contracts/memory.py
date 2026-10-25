"""Memory + object graph contracts (SPEC §32, §52E-G).

Memory is context only. It must never become authority (§32, §52E).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from nomadicos.contracts.core import Contract

MAX_EVIDENCE_REFS = 8
MAX_REF_CHARS = 128
MAX_MATCHED_TOKENS = 16
MAX_TRAJECTORY_STEPS = 32
MAX_STEP_SUMMARY_CHARS = 200
MAX_GOAL_TEXT_CHARS = 300
MAX_OUTCOME_CHARS = 200
MAX_SUPERSESSION_REASON_CHARS = 200


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemorySource(StrEnum):
    """Structured origin classification of a persisted memory item."""

    USER_INPUT = "user_input"
    OBSERVATION = "observation"
    VERIFICATION = "verification"
    FILE = "file"
    REPOSITORY = "repository"
    EVENT = "event"
    TRAJECTORY = "trajectory"
    EXTERNAL = "external"


class MemoryProvenance(Contract):
    """Auditable origin (§52G)."""

    source_task_id: str | None = None
    source_event_id: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    verified: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: what KIND of surface the memory came from (§52G provenance)
    source_type: MemorySource = MemorySource.EVENT
    #: bounded references to supporting evidence (event/execution/verifier ids)
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded_refs(self) -> MemoryProvenance:
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence refs exceed {MAX_EVIDENCE_REFS}")
        self.evidence_refs = [ref[:MAX_REF_CHARS] for ref in self.evidence_refs]
        return self


class MemoryRecord(Contract):
    id: str
    kind: MemoryKind
    content: str
    tags: list[str] = Field(default_factory=list)
    provenance: MemoryProvenance
    #: external text stored here remains data; prompt builders wrap/mark it.
    data_only: bool = True
    #: supersession status: historical records are KEPT, never deleted;
    #: retrieval prefers current (non-superseded) knowledge (§52G).
    superseded: bool = False
    superseded_by: str | None = None

    @model_validator(mode="after")
    def _unverified_never_high_conf(self) -> MemoryRecord:
        if not self.provenance.verified and self.provenance.confidence > 0.95:
            raise ValueError("unverified memory cannot have confidence > 0.95 (§52G)")
        if not self.superseded and self.superseded_by is not None:
            raise ValueError("only a superseded record may carry a superseded_by link")
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
    #: relevance filter: hits scoring below this are dropped (deterministic)
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class RetrievedMemory(Contract):
    record: MemoryRecord | ObjectRecord | RelationRecord
    score: float = Field(ge=0.0, le=1.0)
    #: why this memory was retrieved: the query tokens it matched (bounded)
    matched: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded_matched(self) -> RetrievedMemory:
        if len(self.matched) > MAX_MATCHED_TOKENS:
            raise ValueError(f"matched tokens exceed {MAX_MATCHED_TOKENS}")
        return self


class SupersessionRecord(Contract):
    """Structured supersession fact: which record replaced which, and why.

    Historical provenance: the superseded record is KEPT (never deleted)
    and this record is stored alongside it as evidence of the transition.
    """

    superseded_id: str
    superseded_by: str
    reason: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _coherent(self) -> SupersessionRecord:
        if not self.superseded_id.strip() or not self.superseded_by.strip():
            raise ValueError("supersession requires both record ids")
        if self.superseded_id == self.superseded_by:
            raise ValueError("a record cannot supersede itself")
        self.reason = self.reason[:MAX_SUPERSESSION_REASON_CHARS]
        return self


class TrajectoryStepKind(StrEnum):
    """Structured trajectory step kinds (goal → actions → observations →
    failures → recovery → outcome)."""

    ACTION = "ACTION"
    OBSERVATION = "OBSERVATION"
    FAILURE = "FAILURE"
    RECOVERY = "RECOVERY"
    OUTCOME = "OUTCOME"


class TrajectoryStep(Contract):
    """One bounded, referenced step of a mission trajectory."""

    kind: TrajectoryStepKind
    #: the event/execution/candidate/verifier id this step is traceable to
    ref: str
    summary: str = ""
    at: datetime | None = None

    @model_validator(mode="after")
    def _bounded(self) -> TrajectoryStep:
        if not self.ref.strip():
            raise ValueError("trajectory step requires a source reference")
        self.ref = self.ref[:MAX_REF_CHARS]
        self.summary = self.summary[:MAX_STEP_SUMMARY_CHARS]
        return self


class TrajectoryRecord(Contract):
    """Replayable structured record of what happened during one task.

    Preserves the mission trajectory STRUCTURE (steps with source refs) —
    never only a final natural-language summary. DATA only.
    """

    id: str
    mission_id: str
    task_id: str
    goal_text: str = ""
    steps: list[TrajectoryStep] = Field(default_factory=list)
    outcome: str = ""
    verified: bool = False
    provenance: MemoryProvenance

    @model_validator(mode="after")
    def _bounded(self) -> TrajectoryRecord:
        if not self.mission_id.strip() or not self.task_id.strip():
            raise ValueError("trajectory requires mission and task ids")
        if len(self.steps) > MAX_TRAJECTORY_STEPS:
            raise ValueError(f"trajectory steps exceed {MAX_TRAJECTORY_STEPS}")
        self.goal_text = self.goal_text[:MAX_GOAL_TEXT_CHARS]
        self.outcome = self.outcome[:MAX_OUTCOME_CHARS]
        return self
