"""Task decomposer — MAP core (NomadicBrain spec §13, Sprint 2).

Purpose: *what must be achieved?* The decomposer turns one goal's brain
state into a validated TaskGraph (subgoals, task nodes, explicit
dependencies, success predicates where the model provides them).

Hard rules:

- The decomposer plugs into model inference ONLY through the
  ``StructuredGenerator`` protocol (``brain.cognition``) — no provider,
  no prompt, no HTTP lives here.
- Model output is never trusted: it must parse into
  ``DecompositionPayload`` (closed schema), every id must be safe, and
  the graph is re-validated by ``TaskGraph`` itself (duplicates, unknown
  dependencies, cycles, dangling subgoals, goal binding all fail
  closed).
- The decomposer never executes tools, never authorizes capabilities,
  and never mutates unrelated NomadicOS state: it returns a typed
  ``Decomposition``; applying it to a state is an explicit, validated
  transition (``apply_decomposition``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
    parse_model_json,
)
from nomadicos.brain.errors import InvalidBrainState, InvalidTransition, ModelOutputRejected
from nomadicos.brain.planner.task_graph import MAX_NODES, TaskGraph, TaskNode, is_safe_node_id
from nomadicos.brain.schemas import MAX_DESCRIPTION, Subgoal, canonical_json
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract, GoalPredicate, parse_predicate

MAX_SUBGOALS = 64


def _model_output_rejected(context: str, exc: Exception) -> ModelOutputRejected:
    """Summarize a pydantic/parse failure into one fail-closed error."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:3]:
            loc = ".".join(str(part) for part in error.get("loc", ()))
            parts.append(f"{loc or '<root>'}: {error.get('msg', 'invalid')}")
        return ModelOutputRejected(f"{context}: schema validation failed ({'; '.join(parts)})")
    return ModelOutputRejected(f"{context}: {exc}")


class SubgoalSpec(Contract):
    """One model-proposed subgoal (typed, closed schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    #: machine-checkable completion condition when one is expressible
    success_predicate: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> SubgoalSpec:
        if not is_safe_node_id(self.id):
            raise ValueError(f"unsafe subgoal id {self.id!r}")
        if not self.description.strip():
            raise ValueError("subgoal description must not be empty")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(f"subgoal description exceeds {MAX_DESCRIPTION} chars")
        return self


class TaskSpec(Contract):
    """One model-proposed task node (typed, closed schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    description: str
    subgoal_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    success_predicate: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _coherent(self) -> TaskSpec:
        if not is_safe_node_id(self.id):
            raise ValueError(f"unsafe task id {self.id!r}")
        if not self.description.strip():
            raise ValueError("task description must not be empty")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(f"task description exceeds {MAX_DESCRIPTION} chars")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError(f"task {self.id!r} has duplicate dependencies")
        for dep in self.depends_on:
            if not is_safe_node_id(dep):
                raise ValueError(f"unsafe dependency id {dep!r}")
        if self.subgoal_id is not None and not is_safe_node_id(self.subgoal_id):
            raise ValueError(f"unsafe subgoal reference {self.subgoal_id!r}")
        return self


class DecompositionPayload(Contract):
    """The ONLY shape model text may take as a decomposition.

    Closed schema: extra/unknown keys (including authority-shaped ones)
    fail closed at parse time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subgoals: list[SubgoalSpec] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _bounded(self) -> DecompositionPayload:
        if len(self.subgoals) > MAX_SUBGOALS:
            raise ValueError(f"decomposition exceeds {MAX_SUBGOALS} subgoals")
        if len(self.tasks) > MAX_NODES:
            raise ValueError(f"decomposition exceeds {MAX_NODES} tasks")
        return self


class Decomposition(Contract):
    """A validated decomposition result (data only, no side effects)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_graph: TaskGraph
    rationale: str = ""

    @model_validator(mode="after")
    def _bounded(self) -> Decomposition:
        if len(self.rationale) > MAX_DESCRIPTION:
            raise ValueError(f"decomposition rationale exceeds {MAX_DESCRIPTION} chars")
        return self


@runtime_checkable
class Decomposer(Protocol):
    """Model-independent decomposer seam (spec §13)."""

    async def decompose(self, state: BrainState) -> Decomposition: ...


def decomposition_request_payload(state: BrainState) -> dict[str, Any]:
    """Bounded, deterministic snapshot of the state relevant to planning."""
    facts = {
        key: {"value": fact.value, "kind": fact.kind.value}
        for key, fact in list(state.world.facts.items())[:16]
    }
    return {
        "goal": {
            "id": state.goal.id,
            "objective": state.goal.objective,
            "constraints": list(state.goal.constraints),
            "predicates": [gp.model_dump(mode="json") for gp in state.goal.predicates[:16]],
        },
        "world_facts": facts,
        "mission_constraints": list(state.constraints.items),
    }


def parse_decomposition(text: str, *, goal_id: str, context: str = "decomposer") -> Decomposition:
    """Validate raw model text into a typed Decomposition (fail closed).

    Every structural guarantee (unique ids, dependency validity, no
    cycles, goal binding) is enforced by the TaskGraph contract itself;
    this function only parses, bounds, and forwards. Any deviation from
    the typed schema raises ``ModelOutputRejected``.
    """
    payload = parse_model_json(text, context=context)
    try:
        parsed = DecompositionPayload.model_validate(payload)
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None

    try:
        subgoals = [
            Subgoal(
                id=spec.id,
                goal_id=goal_id,
                description=spec.description,
            )
            for spec in parsed.subgoals
        ]
        nodes = [
            TaskNode(
                id=spec.id,
                description=spec.description,
                subgoal_id=spec.subgoal_id,
                depends_on=list(spec.depends_on),
                success_predicate=_parse_predicate(spec.success_predicate, spec.id),
            )
            for spec in parsed.tasks
        ]
        graph = TaskGraph.build(goal_id=goal_id, nodes=nodes, subgoals=subgoals)
    except (ValueError, ValidationError) as exc:
        raise _model_output_rejected(f"{context} for goal {goal_id!r}", exc) from None
    return Decomposition(task_graph=graph)


def _parse_predicate(raw: dict[str, Any] | None, task_id: str) -> GoalPredicate | None:
    if raw is None:
        return None
    try:
        return parse_predicate(raw)
    except ValueError as exc:
        raise ModelOutputRejected(f"task {task_id!r}: invalid success predicate ({exc})") from None


class ModelDecomposer:
    """StructuredGenerator-backed decomposer (spec §13).

    Model-agnostic: whatever ``StructuredGenerator`` implementation is
    injected (mock for tests, adapter over llama.cpp/Ollama in prod),
    this class behaves identically — propose a request, validate the
    reply, return a typed decomposition.
    """

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def decompose(self, state: BrainState) -> Decomposition:
        if state.task_graph.nodes:
            raise InvalidTransition(
                "brain state is already decomposed; replanning is a later sprint",
                mission_id=state.mission_id,
                goal_id=state.goal.id,
            )
        request = CognitiveRequest(
            role=CognitiveRole.DECOMPOSER,
            mission_id=state.mission_id,
            payload=decomposition_request_payload(state),
        )
        text = await self._generator.generate_structured(request)
        return parse_decomposition(text, goal_id=state.goal.id)


def apply_decomposition(state: BrainState, decomposition: Decomposition) -> BrainState:
    """Transition: replace the (empty) graph with a validated decomposition.

    Re-constructs the full ``BrainState`` so ALL state validators re-run
    (fail closed against goal-binding drift). No other field changes.
    """
    graph = decomposition.task_graph
    if graph.goal_id != state.goal.id:
        raise InvalidBrainState(
            "decomposition does not belong to the state's goal",
            graph_goal=graph.goal_id,
            goal_id=state.goal.id,
        )
    if state.task_graph.nodes:
        raise InvalidTransition(
            "brain state is already decomposed; replanning is a later sprint",
            mission_id=state.mission_id,
        )
    data = state.model_dump()
    data["task_graph"] = graph
    data["state_version"] = state.state_version + 1
    return BrainState.model_validate(data)


def decomposition_fingerprint(decomposition: Decomposition) -> str:
    """Deterministic fingerprint for replay comparison."""
    return canonical_json(decomposition.task_graph)
