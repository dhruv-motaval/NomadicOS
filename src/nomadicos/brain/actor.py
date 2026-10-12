"""Actor — candidate generation (NomadicBrain spec §14, Sprint 2).

Purpose: *what could I do next?* The actor inspects the brain state and
the active subgoal and proposes MULTIPLE typed ``ActionCandidate``
objects with structured arguments, preconditions, expected effects, the
required capability/tool class, and provenance (rationale, refinement
revision, rejected predecessors).

Hard rules:

- Proposals are DATA ONLY: the actor never executes, never produces an
  ``AuthorizedAction``, never touches authority, policy, executor, or
  tools. Execution stays exclusively Action IR -> authority -> executor.
- Model inference enters ONLY through the ``StructuredGenerator``
  protocol; model output must parse into ``ProposalPayload`` (closed
  schema) and pass typed validation, else it fails closed.
- Candidate identity is preserved: model-provided ids are validated and
  kept; revisions are stamped with the iteration and the feedback the
  refinement consumed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
    parse_model_json,
)
from nomadicos.brain.decomposer import _model_output_rejected
from nomadicos.brain.errors import InvalidBrainState, ModelOutputRejected
from nomadicos.brain.planner.task_graph import is_safe_node_id
from nomadicos.brain.schemas import (
    MAX_DESCRIPTION,
    MAX_ITEM_CHARS,
    MAX_PROVENANCE_REFS,
    ActionCandidate,
    CandidateKind,
    MonitorResult,
    Subgoal,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract

MAX_CANDIDATES_PER_PROPOSAL = 8


class CandidateSpec(Contract):
    """One model-proposed candidate (typed, closed schema).

    Invalid kinds, unsafe ids, and unknown fields fail closed at parse
    time — before any ActionCandidate exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: CandidateKind
    subgoal_id: str | None = None
    capability: str = ""
    tool: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_effect: str
    rationale: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> CandidateSpec:
        if not is_safe_node_id(self.id):
            raise ValueError(f"unsafe candidate id {self.id!r}")
        if not self.expected_effect.strip():
            raise ValueError("candidate expected_effect must not be empty")
        if len(self.expected_effect) > MAX_DESCRIPTION:
            raise ValueError(f"candidate expected_effect exceeds {MAX_DESCRIPTION} chars")
        if len(self.rationale) > MAX_DESCRIPTION:
            raise ValueError(f"candidate rationale exceeds {MAX_DESCRIPTION} chars")
        if len(self.capability) > MAX_ITEM_CHARS or len(self.tool) > MAX_ITEM_CHARS:
            raise ValueError("candidate capability/tool must be bounded strings")
        for pre in self.preconditions:
            if not isinstance(pre, str) or not pre.strip():
                raise ValueError("candidate preconditions must be non-empty strings")
        return self


class ProposalPayload(Contract):
    """The ONLY shape model text may take as an actor proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: list[CandidateSpec] = Field(min_length=1, max_length=MAX_CANDIDATES_PER_PROPOSAL)

    @model_validator(mode="after")
    def _unique_ids(self) -> ProposalPayload:
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate id in proposal")
        return self


class ActorProposal(Contract):
    """A typed bundle of ActionCandidates (data only, never authority).

    Provenance: ``iteration`` marks the refinement pass, ``source`` who
    produced it, and each candidate carries ``revision``/``derived_from``
    linking it to the feedback that triggered the refinement.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subgoal_id: str
    iteration: int = Field(ge=1)
    candidates: list[ActionCandidate] = Field(min_length=1, max_length=MAX_CANDIDATES_PER_PROPOSAL)
    source: str = "structured_generator"

    @model_validator(mode="after")
    def _coherent(self) -> ActorProposal:
        if not self.subgoal_id.strip():
            raise ValueError("actor proposal subgoal_id must not be empty")
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate candidate id in proposal")
        for candidate in self.candidates:
            if candidate.subgoal_id != self.subgoal_id:
                raise ValueError(
                    f"candidate {candidate.id!r} does not target the proposal's subgoal"
                )
            if candidate.revision != self.iteration:
                raise ValueError(
                    f"candidate {candidate.id!r} revision {candidate.revision} does not "
                    f"match iteration {self.iteration}"
                )
        return self


@runtime_checkable
class Actor(Protocol):
    """Model-independent actor seam (spec §14). The actor proposes and
    NEVER executes."""

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ) -> ActorProposal: ...


def candidate_request_payload(
    state: BrainState,
    subgoal: Subgoal,
    feedback: Sequence[MonitorResult] = (),
) -> dict[str, Any]:
    """Bounded, deterministic snapshot for candidate generation."""
    return {
        "goal_objective": state.goal.objective,
        "subgoal": {"id": subgoal.id, "description": subgoal.description},
        "capabilities": [c.name for c in state.capabilities[:16]],
        "world_facts": {
            key: {"value": fact.value, "kind": fact.kind.value}
            for key, fact in list(state.world.facts.items())[:16]
        },
        "constraints": list(state.constraints.items),
        "monitor_feedback": [
            {
                "candidate_id": r.candidate_id,
                "decision": r.decision.value,
                "stage": r.stage.value,
                "reasons": list(r.reasons),
            }
            for r in list(feedback)[: MAX_PROVENANCE_REFS * 2]
            if r.decision.value == "REJECTED"
        ],
    }


def parse_proposal(
    text: str,
    *,
    subgoal: Subgoal,
    iteration: int,
    feedback: Sequence[MonitorResult] = (),
    context: str = "actor",
) -> ActorProposal:
    """Validate raw model text into a typed ActorProposal (fail closed)."""
    payload = parse_model_json(text, context=context)
    try:
        parsed = ProposalPayload.model_validate(payload)
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None

    derived_from = sorted({r.candidate_id for r in feedback if r.decision.value == "REJECTED"})
    for spec in parsed.candidates:
        if spec.subgoal_id is not None and spec.subgoal_id != subgoal.id:
            raise ModelOutputRejected(
                f"{context}: candidate {spec.id!r} targets subgoal {spec.subgoal_id!r}, "
                f"not the active subgoal {subgoal.id!r}"
            )
    try:
        candidates = [
            ActionCandidate(
                id=spec.id,
                subgoal_id=subgoal.id,
                kind=spec.kind,
                capability=spec.capability,
                tool=spec.tool,
                arguments=dict(spec.arguments),
                preconditions=list(spec.preconditions),
                expected_effect=spec.expected_effect,
                rationale=spec.rationale,
                revision=iteration,
                derived_from=derived_from[:MAX_PROVENANCE_REFS],
            )
            for spec in parsed.candidates
        ]
    except ValidationError as exc:
        raise _model_output_rejected(f"{context} for subgoal {subgoal.id!r}", exc) from None
    try:
        return ActorProposal(subgoal_id=subgoal.id, iteration=iteration, candidates=candidates)
    except ValidationError as exc:
        raise _model_output_rejected(f"{context} for subgoal {subgoal.id!r}", exc) from None


class ModelActor:
    """StructuredGenerator-backed actor (spec §14).

    Deterministic for identical (state, subgoal, feedback, generator)
    inputs: no ids, timestamps, or randomness are minted here —
    candidate identity comes from validated model output.
    """

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ) -> ActorProposal:
        if subgoal.goal_id != state.goal.id:
            raise InvalidBrainState(
                "actor subgoal does not belong to the state's goal",
                subgoal_id=subgoal.id,
                goal_id=state.goal.id,
            )
        request = CognitiveRequest(
            role=CognitiveRole.ACTOR,
            mission_id=state.mission_id,
            payload=candidate_request_payload(state, subgoal, feedback),
        )
        text = await self._generator.generate_structured(request)
        return parse_proposal(text, subgoal=subgoal, iteration=iteration, feedback=feedback)
