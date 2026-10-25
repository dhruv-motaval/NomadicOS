"""Canonical brain domain objects (NomadicBrain spec §5-8, Sprint 1;
Sprint 3 deliberation data contracts).

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

import hashlib
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
# Sprint 3 — deliberation bounds (hard limits; model output cannot change them)
MAX_PREDICTED_FACTS = 16
MAX_PREDICTIONS = 64
MAX_EVALUATIONS = 64
SEARCH_BRANCHING = 2  #: maximum child branches expanded from any search node (B)
SEARCH_MAX_DEPTH = 2  #: maximum search depth (L)
#: root + B children + B**L grandchildren for the Sprint 3 fixed limits
MAX_SEARCH_NODES = 1 + SEARCH_BRANCHING + SEARCH_BRANCHING**SEARCH_MAX_DEPTH
# Sprint 6 — adaptive intelligence bounds (hard limits; model output cannot
# change them)
MAX_ITERATIONS_BOUND = 16  #: canonical loop/effort iteration ceiling (loop.py imports)
MAX_ATTENTION_ITEMS = 8
MAX_ATTENTION_REASONS = 4
MAX_ATTENTION_TEXT_CHARS = 200
MAX_BELIEF_FACTS = 32
MAX_INFO_REQUESTS = 4
MAX_METACOGNITION_ITEMS = 8


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


def _derived_id(prefix: str, *parts: Any) -> str:
    """Deterministic, collision-safe id derived from canonical inputs.

    SHA-256 over the canonical JSON of the parts (no UUID randomness, no
    wall clock) so identical deliberation inputs always produce identical
    ids and trajectories replay deterministically.
    """
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{prefix}_{digest[:16]}"


class EvaluationFactors(Contract):
    """Explicit component factors of one evaluation (spec Sprint 3).

    Semantics: higher ``goal_progress``/``feasibility``/``reversibility``
    is better; higher ``risk``/``uncertainty``/``resource_cost`` is
    worse. All factors are bounded to [0.0, 1.0].
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_progress: float = Field(ge=0.0, le=1.0)
    feasibility: float = Field(ge=0.0, le=1.0)
    risk: float = Field(ge=0.0, le=1.0)
    uncertainty: float = Field(ge=0.0, le=1.0)
    resource_cost: float = Field(ge=0.0, le=1.0)
    reversibility: float = Field(ge=0.0, le=1.0)


class Prediction(Contract):
    """A HYPOTHETICAL future world if one candidate is performed
    (Sprint 3). DATA only: predictions are never observations, never
    evidence, never verified facts, and never authorize anything.

    Every fact in ``predicted_world`` MUST carry
    ``WorldFactKind.PREDICTION`` — hypothetical facts can never silently
    become normal FACT values (NomadicBrain Sprint 3 §19).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    candidate_id: str
    predicted_world: WorldState
    confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> Prediction:
        if not self.candidate_id.strip():
            raise ValueError("prediction candidate_id must not be empty")
        if not self.id.strip():
            raise ValueError("prediction id must not be empty")
        if len(self.predicted_world.facts) > MAX_PREDICTED_FACTS:
            raise ValueError(f"predicted world exceeds {MAX_PREDICTED_FACTS} facts")
        for key, fact in self.predicted_world.facts.items():
            if fact.kind is not WorldFactKind.PREDICTION:
                raise ValueError(
                    f"predicted fact {key!r} must be {WorldFactKind.PREDICTION.value}, "
                    f"got {fact.kind.value} (hypothetical facts never become FACT)"
                )
        for label in ("assumptions", "uncertainties"):
            items = getattr(self, label)
            if len(items) > MAX_CONSTRAINTS:
                raise ValueError(f"prediction {label} exceed {MAX_CONSTRAINTS} items")
            for item in items:
                if not item.strip():
                    raise ValueError(f"prediction {label} items must not be empty")
                if len(item) > MAX_ITEM_CHARS:
                    raise ValueError(f"prediction {label} item exceeds {MAX_ITEM_CHARS} chars")
        return self


class Evaluation(Contract):
    """Estimated utility of one predicted future relative to the goal
    (Sprint 3). NOT verification, NOT ground truth, NOT authorization —
    a planning-time estimate with explicit component factors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    candidate_id: str
    prediction_id: str
    score: float = Field(ge=0.0, le=1.0)
    factors: EvaluationFactors
    rationale: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> Evaluation:
        if not self.candidate_id.strip():
            raise ValueError("evaluation candidate_id must not be empty")
        if not self.prediction_id.strip():
            raise ValueError("evaluation prediction_id must not be empty")
        if not self.id.strip():
            raise ValueError("evaluation id must not be empty")
        if len(self.rationale) > MAX_DESCRIPTION:
            raise ValueError(f"evaluation rationale exceeds {MAX_DESCRIPTION} chars")
        return self


class SearchNodeStatus(StrEnum):
    """Status of one deliberative search node (Sprint 3)."""

    EXPANDED = "EXPANDED"
    TERMINAL = "TERMINAL"
    FAILED = "FAILED"


class SearchStatus(StrEnum):
    """Outcome of one bounded deliberative search. No implicit success."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SearchNode(Contract):
    """One node of the bounded deliberative search tree (Sprint 3).

    ``node_id`` encodes the search path deterministically (``root``,
    ``root.0``, ``root.0.0``, ...). ``evaluation_score``/``confidence``/
    ``risk`` are denormalized from the referenced Prediction/Evaluation
    so the result is self-contained for replay, selection, and
    persistence. A FAILED node has no evaluation and is unselectable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(max_length=MAX_NODE_ID)
    parent_node_id: str | None = None
    depth: int = Field(ge=0, le=SEARCH_MAX_DEPTH)
    candidate_id: str | None = None
    prediction_id: str | None = None
    evaluation_id: str | None = None
    child_node_ids: list[str] = Field(default_factory=list)
    terminal: bool = False
    status: SearchNodeStatus
    propagated_value: float | None = None
    evaluation_score: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    risk: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> SearchNode:
        if not self.node_id.strip():
            raise ValueError("search node_id must not be empty")
        if len(self.child_node_ids) > SEARCH_BRANCHING:
            raise ValueError(f"search node exceeds {SEARCH_BRANCHING} child branches")
        for child in self.child_node_ids:
            if not child.strip():
                raise ValueError("search node child references must not be empty")
        if self.status is SearchNodeStatus.FAILED and not self.terminal:
            raise ValueError("a FAILED search node is terminal (never expanded)")
        return self


class SearchResult(Contract):
    """Typed result of one bounded deliberative search (Sprint 3).

    Retains the full tree for replay, deterministic selection,
    debugging, state persistence, and event tracing. Bounded by the
    Sprint 3 hard limits (B=2, L=2 -> at most
    ``MAX_SEARCH_NODES`` nodes). Data only: never an execution, never
    authority, and COMPLETED is a search outcome — never mission
    success.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_node_id: str
    root_candidate_ids: list[str] = Field(default_factory=list)
    nodes: list[SearchNode] = Field(default_factory=list)
    status: SearchStatus = SearchStatus.COMPLETED
    failure_reason: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> SearchResult:
        if not self.root_node_id.strip():
            raise ValueError("search result root_node_id must not be empty")
        if len(self.root_candidate_ids) > SEARCH_BRANCHING:
            raise ValueError(f"search result exceeds {SEARCH_BRANCHING} root branches (B limit)")
        if len(self.nodes) > MAX_SEARCH_NODES:
            raise ValueError(f"search result exceeds {MAX_SEARCH_NODES} nodes")
        if len(self.failure_reason) > MAX_DESCRIPTION:
            raise ValueError(f"search failure reason exceeds {MAX_DESCRIPTION} chars")
        if self.status is SearchStatus.FAILED and not self.failure_reason.strip():
            raise ValueError("a FAILED search result requires an explicit reason")
        node_ids = {n.node_id for n in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("duplicate search node id in result")
        for node in self.nodes:
            for child in node.child_node_ids:
                if child not in node_ids:
                    raise ValueError(
                        f"search node {node.node_id!r} references unknown child {child!r}"
                    )
        return self


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


# ══════════════════════════ Sprint 6 — Adaptive Intelligence ══════════════════════════


class AttentionSource(StrEnum):
    """What kind of cognitive item attention selected (Sprint 6)."""

    SUBGOAL = "SUBGOAL"
    OBSERVATION = "OBSERVATION"
    HYPOTHESIS = "HYPOTHESIS"
    WORLD_FACT = "WORLD_FACT"
    MEMORY = "MEMORY"
    ACTION_CANDIDATE = "ACTION_CANDIDATE"


class AttentionItem(Contract):
    """One selected cognitive item with its deterministic salience score
    and bounded reasons (Sprint 6, spec §9). DATA only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(max_length=MAX_NODE_ID)
    source: AttentionSource
    summary: str = ""
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> AttentionItem:
        if not self.ref.strip():
            raise ValueError("attention item ref must not be empty")
        if len(self.summary) > MAX_ATTENTION_TEXT_CHARS:
            raise ValueError(f"attention summary exceeds {MAX_ATTENTION_TEXT_CHARS} chars")
        if len(self.reasons) > MAX_ATTENTION_REASONS:
            raise ValueError(f"attention reasons exceed {MAX_ATTENTION_REASONS}")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("attention reasons must not be empty")
        return self


class AttentionView(Contract):
    """Focused cognitive context: the bounded, deterministically ordered
    top items attention selected (Sprint 6). Ordering (-score, ref) is
    enforced at construction — the view is always canonical."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[AttentionItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> AttentionView:
        if len(self.items) > MAX_ATTENTION_ITEMS:
            raise ValueError(f"attention view exceeds {MAX_ATTENTION_ITEMS} items")
        ordered = sorted(self.items, key=lambda i: (-i.score, i.ref))[:MAX_ATTENTION_ITEMS]
        if ordered != self.items:
            raise ValueError("attention items must be in deterministic (-score, ref) order")
        return self


class UncertaintyLevel(StrEnum):
    """Epistemic uncertainty level of one belief (Sprint 6, spec §8).

    Distinguishes verified, confident, uncertain, conflicting, unknown,
    and stale — never reduced to a single asserted state."""

    VERIFIED = "VERIFIED"
    CONFIDENT = "CONFIDENT"
    UNCERTAIN = "UNCERTAIN"
    CONFLICTING = "CONFLICTING"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"


class FactUncertainty(Contract):
    """Uncertainty of one world fact key (Sprint 6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    level: UncertaintyLevel
    #: belief confidence in [0, 1] (1.0 = certain, 0.0 = unknown)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> FactUncertainty:
        if not self.key.strip():
            raise ValueError("fact uncertainty key must not be empty")
        return self


class BeliefAssessment(Contract):
    """Deterministic uncertainty assessment of the current world model
    (Sprint 6, spec §8). Derived DATA — never mutates the world it
    assesses and never asserts uncertain reality as fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: list[FactUncertainty] = Field(default_factory=list)
    #: overall uncertainty in [0, 1]: 0 = fully verified, 1 = fully unknown
    overall_uncertainty: float = Field(ge=0.0, le=1.0)
    unverified_hypotheses: int = Field(ge=0)
    conflicting_hypotheses: int = Field(ge=0)
    verified_facts: int = Field(ge=0)
    #: fact keys whose value is UNKNOWN — what is missing
    unknown_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self) -> BeliefAssessment:
        if len(self.facts) > MAX_BELIEF_FACTS:
            raise ValueError(f"belief assessment exceeds {MAX_BELIEF_FACTS} facts")
        if len(self.unknown_keys) > MAX_BELIEF_FACTS:
            raise ValueError(f"belief assessment unknown keys exceed {MAX_BELIEF_FACTS}")
        for key in self.unknown_keys:
            if not key.strip():
                raise ValueError("unknown keys must not be empty")
        return self


class MetacognitiveRecommendation(StrEnum):
    """Advisory next-step recommendation (Sprint 6). NEVER a decision,
    never authorization, never execution."""

    CONTINUE = "CONTINUE"
    GATHER_INFO = "GATHER_INFO"
    SEARCH_DEEPER = "SEARCH_DEEPER"
    STOP = "STOP"


class MetacognitiveAssessment(Contract):
    """Bounded metacognitive self-assessment (Sprint 6): what we believe,
    what supports it, what is uncertain, what is missing, whether to
    continue. Advisory DATA only — it cannot authorize, execute, or
    verify anything."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    believed: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    continue_reasoning: bool
    recommendation: MetacognitiveRecommendation
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bounded(self) -> MetacognitiveAssessment:
        for field_name in ("believed", "uncertainties", "missing", "reasons"):
            items = getattr(self, field_name)
            if len(items) > MAX_METACOGNITION_ITEMS:
                raise ValueError(f"metacognition {field_name} exceed {MAX_METACOGNITION_ITEMS}")
            for item in items:
                if not item.strip():
                    raise ValueError(f"metacognition {field_name} must not be empty")
                if len(item) > MAX_ITEM_CHARS:
                    raise ValueError(f"metacognition {field_name} item exceeds {MAX_ITEM_CHARS}")
        if len(self.evidence_refs) > MAX_PROVENANCE_REFS:
            raise ValueError(f"metacognition evidence exceeds {MAX_PROVENANCE_REFS} refs")
        return self


class InformationRequest(Contract):
    """One bounded information-gathering opportunity (Sprint 6). DATA
    only: a request never executes anything — it becomes a normal
    OBSERVE candidate that travels the standard monitor/authority/
    executor pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    target: str
    rationale: str = ""
    priority: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> InformationRequest:
        if not self.id.strip():
            raise ValueError("information request id must not be empty")
        if not self.target.strip():
            raise ValueError("information request target must not be empty")
        if len(self.target) > MAX_NODE_ID:
            raise ValueError(f"information request target exceeds {MAX_NODE_ID} chars")
        if len(self.rationale) > MAX_ITEM_CHARS:
            raise ValueError(f"information rationale exceeds {MAX_ITEM_CHARS} chars")
        return self


class EffortDecision(StrEnum):
    """Adaptive effort verdict for the next planning step (Sprint 6)."""

    FULL_EFFORT = "FULL_EFFORT"
    REDUCED_EFFORT = "REDUCED_EFFORT"
    GATHER_INFO = "GATHER_INFO"
    STOP = "STOP"


class EffortPlan(Contract):
    """Explicit bounded effort/budget plan (Sprint 6). All outputs are
    hard-bounded by existing Sprint constants — adaptive effort can
    never become an unbounded loop. DATA only: advisory input to the
    existing deterministic loops, never an override of budget or
    authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: EffortDecision
    max_iterations: int = Field(ge=1, le=MAX_ITERATIONS_BOUND)
    search_depth: int = Field(ge=0, le=SEARCH_MAX_DEPTH)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> EffortPlan:
        if len(self.reasons) > MAX_METACOGNITION_ITEMS:
            raise ValueError(f"effort reasons exceed {MAX_METACOGNITION_ITEMS}")
        for reason in self.reasons:
            if not reason.strip():
                raise ValueError("effort reasons must not be empty")
            if len(reason) > MAX_ITEM_CHARS:
                raise ValueError(f"effort reason exceeds {MAX_ITEM_CHARS} chars")
        return self
