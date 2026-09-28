"""Canonical brain domain objects (NomadicBrain spec §5-8, Sprint 1).

Typed, frozen DATA contracts for the cognitive substrate — structure
only: no cognitive logic, no model calls, no tool execution, no
authority. Objects that already exist as NomadicOS contracts are REUSED,
never duplicated:

- Goal               -> nomadicos.contracts.core.Goal
- Observation        -> nomadicos.contracts.execution.Observation
- Evidence           -> nomadicos.contracts.verification.EvidenceItem
- VerificationResult -> nomadicos.contracts.verification.VerificationResult
- Failure            -> nomadicos.contracts.core.FailureRecord

Every object defined here is deterministic by construction: closed
schemas (``extra="forbid"`` — model-authored authority fields such as
``authorized`` are rejected structurally), explicit schema version,
stable identifiers, safe defaults, and canonical JSON serialization
suitable for equality tests and replay. Brain objects are immutable;
state transitions produce new instances.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id

#: schema version of the brain foundation; mismatches fail closed
BRAIN_SCHEMA_VERSION = 1

MAX_DESCRIPTION = 2000
MAX_ITEM_CHARS = 500
MAX_CONSTRAINTS = 20
MAX_WORKING_MEMORY_ITEMS = 16
MAX_RECOVERY_STEPS = 16
MAX_NODE_ID = 128
MAX_CANDIDATES = 64
MAX_PROVENANCE_REFS = 8
MAX_ARGUMENTS_JSON_CHARS = 4096
MAX_PLAN_CANDIDATES = 128
MAX_MONITOR_RESULTS = 256


def canonical_json(model: BaseModel) -> str:
    """Deterministic serialization: sorted keys, no insignificant space."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class WorldFactKind(StrEnum):
    """Epistemic status of one world belief (spec §7)."""

    FACT = "FACT"
    UNKNOWN = "UNKNOWN"
    HYPOTHESIS = "HYPOTHESIS"
    PREDICTION = "PREDICTION"
    VERIFIED_FACT = "VERIFIED_FACT"
    STALE_FACT = "STALE_FACT"


class WorldFact(Contract):
    """One world belief with an explicit epistemic status (spec §7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    value: Any = None
    kind: WorldFactKind = WorldFactKind.FACT

    @model_validator(mode="after")
    def _key_nonempty(self) -> WorldFact:
        if not self.key.strip():
            raise ValueError("world fact key must not be empty")
        return self


class WorldState(Contract):
    """What the system currently believes about the external environment.

    Phase 1 carries only flat, typed facts; relationships between world
    objects belong to the object-graph memory brick (Phase 11) and later
    brain sprints.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: bumped deterministically on every world update
    version: int = Field(default=0, ge=0)
    facts: dict[str, WorldFact] = Field(default_factory=dict)


class WorkingMemoryView(Contract):
    """Serializable value snapshot of the ephemeral working context.

    The mutable, task-isolated *service* already exists as
    ``nomadicos.memory.working.WorkingMemory`` (Phase 11B); BrainState
    carries only this bounded, deterministic value view of it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self) -> WorkingMemoryView:
        if len(self.notes) > MAX_WORKING_MEMORY_ITEMS:
            raise ValueError(f"working memory exceeds {MAX_WORKING_MEMORY_ITEMS} notes")
        for note in self.notes:
            if not note.strip():
                raise ValueError("working memory notes must not be empty")
            if len(note) > MAX_ITEM_CHARS:
                raise ValueError(f"working memory note exceeds {MAX_ITEM_CHARS} chars")
        return self


class MemoryRef(Contract):
    """Reference to a persisted memory item (id + retrieval score).

    BrainState holds references, never full memory records: memory is
    context only (NomadicOS SPEC §32).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str
    score: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _id_nonempty(self) -> MemoryRef:
        if not self.memory_id.strip():
            raise ValueError("memory id must not be empty")
        return self


class Capability(Contract):
    """A typed capability the brain may route to (spec §23, minimal).

    Phase 1 carries name/description only; input/output schemas, risk, and
    verification metadata land with the capability registry sprint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str = ""

    @model_validator(mode="after")
    def _name_nonempty(self) -> Capability:
        if not self.name.strip():
            raise ValueError("capability name must not be empty")
        return self


class Constraints(Contract):
    """Mission-level constraints — owner rules as data, never authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self) -> Constraints:
        if len(self.items) > MAX_CONSTRAINTS:
            raise ValueError(f"constraints exceed {MAX_CONSTRAINTS} items")
        for item in self.items:
            if not item.strip():
                raise ValueError("constraint items must not be empty")
            if len(item) > MAX_ITEM_CHARS:
                raise ValueError(f"constraint item exceeds {MAX_ITEM_CHARS} chars")
        return self


class BudgetState(Contract):
    """Deterministic execution budget (contracts only in Phase 1).

    Hard control (NomadicOS SPEC §1.5): budgets are typed data the
    runtime enforces deterministically; no model output ever adjusts
    them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=32, ge=1)
    steps_used: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _within_budget(self) -> BudgetState:
        if self.steps_used > self.max_steps:
            raise ValueError("steps_used exceeds max_steps")
        return self


class Subgoal(Contract):
    """One measurable intermediate objective under the goal (spec §12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("subgoal"))
    goal_id: str
    description: str

    @model_validator(mode="after")
    def _coherent(self) -> Subgoal:
        if not self.goal_id.strip():
            raise ValueError("subgoal goal_id must not be empty")
        if not self.description.strip():
            raise ValueError("subgoal description must not be empty")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(f"subgoal description exceeds {MAX_DESCRIPTION} chars")
        return self


class Hypothesis(Contract):
    """One causal/explanatory hypothesis with confidence and evidence
    references (spec §8). Confidence must be explicit — no silent
    epistemic defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("hyp"))
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    conflicting_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> Hypothesis:
        if not self.statement.strip():
            raise ValueError("hypothesis statement must not be empty")
        if len(self.statement) > MAX_DESCRIPTION:
            raise ValueError(f"hypothesis statement exceeds {MAX_DESCRIPTION} chars")
        if len(self.supporting_evidence) > MAX_CONSTRAINTS or (
            len(self.conflicting_evidence) > MAX_CONSTRAINTS
        ):
            raise ValueError(f"hypothesis evidence references exceed {MAX_CONSTRAINTS}")
        return self


class CandidateKind(StrEnum):
    """What a candidate action intends (spec §14): gathering information
    can be better than immediately changing the world."""

    EXECUTE = "EXECUTE"
    OBSERVE = "OBSERVE"
    QUERY = "QUERY"
    EXPERIMENT = "EXPERIMENT"
    WAIT = "WAIT"


class ActionCandidate(Contract):
    """A proposed next action (spec §14). DATA ONLY.

    The brain never executes candidates: execution flows only through the
    existing NomadicOS boundaries — Action IR -> authorization ->
    executor (NomadicOS SPEC §19-21). Closed schema: model-authored
    authority fields (``authorized``, ``allowed``, ...) are rejected
    structurally, exactly like ActionProposal.

    Sprint 2 provenance: ``revision`` is the refinement pass that
    proposed the candidate, ``derived_from`` carries the rejected
    candidate ids the feedback came from, ``rationale`` records why it
    was proposed. Provenance is DATA — never authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("cand"))
    subgoal_id: str | None = None
    kind: CandidateKind = CandidateKind.EXECUTE
    capability: str = ""
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_effect: str = ""
    rationale: str = ""
    revision: int = Field(default=1, ge=1)
    derived_from: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> ActionCandidate:
        if self.kind is CandidateKind.EXECUTE and not (
            self.tool.strip() or self.capability.strip()
        ):
            raise ValueError("EXECUTE candidate requires a tool or capability")
        if len(self.preconditions) > MAX_CONSTRAINTS:
            raise ValueError(f"candidate preconditions exceed {MAX_CONSTRAINTS}")
        for pre in self.preconditions:
            if not pre.strip():
                raise ValueError("candidate preconditions must not be empty")
            if len(pre) > MAX_ITEM_CHARS:
                raise ValueError(f"candidate precondition exceeds {MAX_ITEM_CHARS} chars")
        if len(self.expected_effect) > MAX_DESCRIPTION:
            raise ValueError(f"candidate expected_effect exceeds {MAX_DESCRIPTION} chars")
        if len(self.rationale) > MAX_DESCRIPTION:
            raise ValueError(f"candidate rationale exceeds {MAX_DESCRIPTION} chars")
        if len(self.derived_from) > MAX_PROVENANCE_REFS:
            raise ValueError(f"candidate provenance exceeds {MAX_PROVENANCE_REFS} refs")
        for ref in self.derived_from:
            if not ref.strip():
                raise ValueError("candidate provenance refs must not be empty")
        return self


class Decision(Contract):
    """A selection outcome with retained component factors (spec §20).

    ``factors`` keeps the decision evidence (goal progress, risk,
    uncertainty, cost, ...) instead of collapsing it into one opaque
    score. Phase 1 stores factors as typed data only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("dec"))
    candidate_id: str
    selected: bool
    rationale: str = ""
    factors: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _candidate_nonempty(self) -> Decision:
        if not self.candidate_id.strip():
            raise ValueError("decision candidate_id must not be empty")
        return self


class RecoveryPlan(Contract):
    """A bounded plan to recover from a typed failure (spec §29).

    Data only in Phase 1: recovery *execution* is a later sprint and
    stays bounded (attempt/time/state budgets, loop detection).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("rec"))
    failure_id: str
    steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> RecoveryPlan:
        if not self.failure_id.strip():
            raise ValueError("recovery plan failure_id must not be empty")
        if len(self.steps) > MAX_RECOVERY_STEPS:
            raise ValueError(f"recovery plan exceeds {MAX_RECOVERY_STEPS} steps")
        for step in self.steps:
            if not step.strip():
                raise ValueError("recovery plan steps must not be empty")
            if len(step) > MAX_ITEM_CHARS:
                raise ValueError(f"recovery step exceeds {MAX_ITEM_CHARS} chars")
        return self


class MonitorDecision(StrEnum):
    """Explicit monitor verdict (spec §15): no implicit approval exists."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class MonitorStage(StrEnum):
    """Which monitor layer produced the verdict (spec §15 two stages)."""

    STRUCTURAL = "STRUCTURAL"
    SEMANTIC = "SEMANTIC"


class MonitorResult(Contract):
    """One explicit monitoring decision for one candidate (spec §15).

    DATA only: a MonitorResult never authorizes anything — execution
    still flows exclusively through Action IR -> authority -> executor.
    A REJECTED verdict MUST carry at least one explicit reason; closed
    schema rejects authority-shaped fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    decision: MonitorDecision
    stage: MonitorStage
    reasons: list[str] = Field(default_factory=list)
    iteration: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _coherent(self) -> MonitorResult:
        if not self.candidate_id.strip():
            raise ValueError("monitor result candidate_id must not be empty")
        if len(self.reasons) > MAX_CONSTRAINTS:
            raise ValueError(f"monitor result reasons exceed {MAX_CONSTRAINTS}")
        if self.decision is MonitorDecision.REJECTED and not self.reasons:
            raise ValueError("a REJECTED monitor result requires an explicit reason")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("monitor result reasons must not be empty")
            if len(reason) > MAX_ITEM_CHARS:
                raise ValueError(f"monitor reason exceeds {MAX_ITEM_CHARS} chars")
        return self
