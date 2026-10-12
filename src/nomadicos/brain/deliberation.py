"""Deliberation — Predictor, Evaluator, bounded search, deterministic
selector (NomadicBrain Sprint 3).

Purpose: *what is likely to happen, and how useful is it?* The
deliberation layer sits between monitor-gated candidates and the final
planning Decision::

    Actor -> Monitor (gate) -> Predictor -> Evaluator
        -> bounded deterministic search -> deterministic selector
        -> Decision

Hard rules:

- Predictions are HYPOTHETICAL: ``predicted_world`` facts carry
  ``WorldFactKind.PREDICTION`` and never silently become FACT values.
  The original ``BrainState`` is never mutated; branch expansion runs
  against fully re-validated hypothetical snapshots that never become
  real mission state.
- Model inference enters ONLY through ``StructuredGenerator`` /
  ``CognitiveRequest``; model output must parse into closed typed
  schemas, else it fails closed. Prompt wording lives only in the
  inference adapter (``nomadicos.inference.structured``), never here.
- Search is bounded and deterministic: ``SEARCH_BRANCHING`` (B=2) valid
  child branches per node, ``SEARCH_MAX_DEPTH`` (L=2) maximum depth —
  module constants, not configuration, so model output cannot change
  them. Plain depth-first traversal: no MCTS, no rollouts, no sampling,
  no learned policy.
- The selector is a pure deterministic function of the
  ``SearchResult``: no model call, no execution, no authorization, no
  verification, no randomness. Its Decision means "selected by the
  deliberation layer" — never authorized, never executable, never
  verified.

Backpropagation (deterministic, fixed): a leaf's value is its own
``evaluation.score``; a parent's value is the MAXIMUM propagated child
value; a node with no valid child values falls back to its own
``evaluation.score``. A FAILED node (predictor/evaluator failure) has no
value and is unselectable — failures never fabricate values.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, ValidationError, model_validator

from nomadicos.brain.actor import Actor
from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
    parse_model_json,
)
from nomadicos.brain.decomposer import _model_output_rejected
from nomadicos.brain.errors import BrainError, DeliberationError, InvalidBrainState
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.loop import PlanningFailure, PlanningOutcome, PlanningStage, _failure_reason
from nomadicos.brain.monitor import Monitor, MonitorDecision
from nomadicos.brain.schemas import (
    MAX_DESCRIPTION,
    MAX_PREDICTED_FACTS,
    SEARCH_BRANCHING,
    SEARCH_MAX_DEPTH,
    ActionCandidate,
    Decision,
    Evaluation,
    EvaluationFactors,
    Prediction,
    SearchNode,
    SearchNodeStatus,
    SearchResult,
    SearchStatus,
    Subgoal,
    WorldState,
    _derived_id,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id


class PredictionPayload(Contract):
    """The ONLY shape model text may take as a prediction (closed schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    predicted_world: WorldState
    confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class EvaluationPayload(Contract):
    """The ONLY shape model text may take as an evaluation (closed schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    factors: EvaluationFactors
    rationale: str = ""


class AdaptiveSearchPolicy(Contract):
    """Sprint 6 adaptive-search parameters. All values are HARD-BOUNDED:
    depth can never exceed the Sprint 3 constant ``SEARCH_MAX_DEPTH`` and
    branching is untouched (B=2). A low-uncertainty/high-confidence case
    terminates earlier; a high-uncertainty case may use the full (still
    strictly bounded) depth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: evaluation score at/above which a node is good enough — its branch
    #: terminates there (early termination, deterministic)
    stop_score: float = Field(default=0.9, ge=0.0, le=1.0)
    #: prediction confidence required together with stop_score
    stop_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    #: effective maximum depth for this run (capped by SEARCH_MAX_DEPTH)
    max_depth: int = Field(default=SEARCH_MAX_DEPTH, ge=0, le=SEARCH_MAX_DEPTH)


class DeliberationOutcome(Contract):
    """Explicit result of one deliberation run (data only).

    Neither status executes anything and neither is mission SUCCESS: the
    outcome carries the search result and — on success — the selector's
    Decision; on failure a structured reason. Monitor acceptance was
    proposal-gate information; the final action-selection Decision comes
    from the selector, never from a monitor verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SearchStatus
    search: SearchResult
    decision: Decision | None = None
    failure: PlanningFailure | None = None

    @model_validator(mode="after")
    def _coherent(self) -> DeliberationOutcome:
        if self.status is SearchStatus.COMPLETED:
            if self.failure is not None:
                raise ValueError("a COMPLETED deliberation must not carry a failure")
            if self.decision is None:
                raise ValueError("a COMPLETED deliberation requires a selected decision")
        else:
            if self.failure is None or not self.failure.reason.strip():
                raise ValueError("a FAILED deliberation requires a structured failure reason")
            if self.decision is not None:
                raise ValueError("a FAILED deliberation must not carry a decision")
        return self


def parse_prediction(
    text: str,
    *,
    candidate_id: str,
    context: str = "predictor",
) -> Prediction:
    """Validate raw model text into a typed Prediction (fail closed).

    Empty output, prose, fences, malformed/non-object JSON, unknown
    fields, oversized values, invalid confidence, and non-hypothetical
    prediction facts are all rejected — never repaired, never partially
    accepted, never fabricated. The prediction id is derived
    deterministically from the validated content (no wall clock, no
    randomness).
    """
    payload = parse_model_json(text, context=context)
    try:
        parsed = PredictionPayload.model_validate(payload)
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None
    try:
        return Prediction(
            id=_derived_prediction_id(candidate_id, parsed),
            candidate_id=candidate_id,
            predicted_world=parsed.predicted_world,
            confidence=parsed.confidence,
            assumptions=list(parsed.assumptions),
            uncertainties=list(parsed.uncertainties),
        )
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None


def parse_evaluation(
    text: str,
    *,
    candidate_id: str,
    prediction_id: str,
    context: str = "evaluator",
) -> Evaluation:
    """Validate raw model text into a typed Evaluation (fail closed).

    Malformed evaluator output, invalid bounds, unknown fields, and
    authority-shaped fields all fail closed; the evaluation id is
    derived deterministically from the validated content.
    """
    payload = parse_model_json(text, context=context)
    try:
        parsed = EvaluationPayload.model_validate(payload)
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None
    try:
        return Evaluation(
            id=_derived_evaluation_id(candidate_id, prediction_id, parsed),
            candidate_id=candidate_id,
            prediction_id=prediction_id,
            score=parsed.score,
            factors=parsed.factors,
            rationale=parsed.rationale,
        )
    except ValidationError as exc:
        raise _model_output_rejected(context, exc) from None


def _derived_prediction_id(candidate_id: str, parsed: PredictionPayload) -> str:
    return _derived_id(
        "pred",
        candidate_id,
        parsed.predicted_world.model_dump(mode="json"),
        parsed.confidence,
        parsed.assumptions,
        parsed.uncertainties,
    )


def _derived_evaluation_id(
    candidate_id: str, prediction_id: str, parsed: EvaluationPayload
) -> str:
    return _derived_id(
        "eval",
        candidate_id,
        prediction_id,
        parsed.score,
        parsed.factors.model_dump(mode="json"),
    )


def prediction_request_payload(
    state: BrainState,
    candidate: ActionCandidate,
    subgoal: Subgoal | None = None,
) -> dict[str, Any]:
    """Bounded, deterministic prediction snapshot: mission goal, active
    subgoal, current world facts, the candidate, capabilities, and
    constraints. No raw conversation, no credentials, no runtime
    objects, no unrelated system state."""
    active = subgoal if subgoal is not None else state.active_subgoal
    return {
        "goal_objective": state.goal.objective,
        "active_subgoal": (
            {"id": active.id, "description": active.description} if active else None
        ),
        "world_facts": {
            key: {"value": fact.value, "kind": fact.kind.value}
            for key, fact in list(state.world.facts.items())[:16]
        },
        "candidate": candidate.model_dump(mode="json"),
        "capabilities": [c.name for c in state.capabilities[:16]],
        "constraints": list(state.constraints.items),
    }


def evaluation_request_payload(
    state: BrainState,
    candidate: ActionCandidate,
    prediction: Prediction,
    subgoal: Subgoal | None = None,
) -> dict[str, Any]:
    """Bounded, deterministic evaluation snapshot: the goal, the
    candidate, its prediction, and the required planning context."""
    active = subgoal if subgoal is not None else state.active_subgoal
    return {
        "goal_objective": state.goal.objective,
        "active_subgoal": (
            {"id": active.id, "description": active.description} if active else None
        ),
        "world_facts": {
            key: {"value": fact.value, "kind": fact.kind.value}
            for key, fact in list(state.world.facts.items())[:16]
        },
        "candidate": candidate.model_dump(mode="json"),
        "prediction": {
            "confidence": prediction.confidence,
            "predicted_facts": {
                key: {"value": fact.value, "kind": fact.kind.value}
                for key, fact in list(prediction.predicted_world.facts.items())[
                    :MAX_PREDICTED_FACTS
                ]
            },
            "assumptions": list(prediction.assumptions),
            "uncertainties": list(prediction.uncertainties),
        },
        "constraints": list(state.constraints.items),
    }


@runtime_checkable
class Predictor(Protocol):
    """Model-independent predictor seam (Sprint 3): hypothetical futures."""

    async def predict(
        self, state: BrainState, candidate: ActionCandidate, subgoal: Subgoal | None = None
    ) -> Prediction: ...


@runtime_checkable
class Evaluator(Protocol):
    """Model-independent evaluator seam (Sprint 3): estimated utility of
    a predicted future relative to the goal — never verification."""

    async def evaluate(
        self,
        state: BrainState,
        candidate: ActionCandidate,
        prediction: Prediction,
        subgoal: Subgoal | None = None,
    ) -> Evaluation: ...


class ModelPredictor:
    """StructuredGenerator-backed predictor (Sprint 3).

    Model-agnostic: whatever ``StructuredGenerator`` implementation is
    injected (mock for tests, adapter over llama.cpp/Ollama in prod),
    this class behaves identically — request a bounded snapshot,
    validate the reply, return a typed Prediction.
    """

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def predict(
        self, state: BrainState, candidate: ActionCandidate, subgoal: Subgoal | None = None
    ) -> Prediction:
        active = subgoal if subgoal is not None else state.active_subgoal
        if (
            candidate.subgoal_id is not None
            and active is not None
            and candidate.subgoal_id != active.id
        ):
            raise InvalidBrainState(
                "predictor candidate does not target the active subgoal",
                candidate_subgoal_id=candidate.subgoal_id,
                active_subgoal_id=active.id,
            )
        request = CognitiveRequest(
            role=CognitiveRole.PREDICTOR,
            mission_id=state.mission_id,
            payload=prediction_request_payload(state, candidate, active),
        )
        text = await self._generator.generate_structured(request)
        return parse_prediction(text, candidate_id=candidate.id)


class ModelEvaluator:
    """StructuredGenerator-backed evaluator (Sprint 3).

    Model-agnostic like ``ModelPredictor``: request, validate, return a
    typed Evaluation. The evaluator never verifies and never
    authorizes; its score is a planning-time estimate.
    """

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def evaluate(
        self,
        state: BrainState,
        candidate: ActionCandidate,
        prediction: Prediction,
        subgoal: Subgoal | None = None,
    ) -> Evaluation:
        request = CognitiveRequest(
            role=CognitiveRole.EVALUATOR,
            mission_id=state.mission_id,
            payload=evaluation_request_payload(state, candidate, prediction, subgoal),
        )
        text = await self._generator.generate_structured(request)
        return parse_evaluation(
            text, candidate_id=candidate.id, prediction_id=prediction.id
        )


def branch_state_from_prediction(state: BrainState, prediction: Prediction) -> BrainState:
    """Hypothetical branch snapshot (Sprint 3).

    The prediction's ``predicted_world`` becomes the branch snapshot's
    world. FULL validation: the state is re-constructed so every
    validator re-runs (never bypassed for convenience). The real
    ``BrainState`` is never mutated, and the snapshot is temporary and
    hypothetical — it never becomes real mission state, never an
    observation, never evidence.
    """
    data = state.model_dump()
    data["world"] = prediction.predicted_world
    data["state_version"] = state.state_version + 1
    return BrainState.model_validate(data)


def select_action(result: SearchResult) -> Decision:
    """Deterministic action selection from a search result (Sprint 3).

    Pure function of the result: no model call, no execution, no
    authorization, no verification, no randomness — identical input
    always produces an identical Decision. Selectable nodes are the
    direct children of the root whose predictor/evaluator succeeded: the
    selected candidate is a real-state, monitor-gated proposal, while
    deeper candidates assume hypothetical parent actions. Ordering:

    1. highest propagated value
    2. highest own evaluation score
    3. highest prediction confidence
    4. lower risk
    5. stable search path (discovery) order
    """
    if result.status is not SearchStatus.COMPLETED:
        raise DeliberationError(
            f"cannot select from a {result.status.value} search result",
            root_node_id=result.root_node_id,
        )
    root = next((n for n in result.nodes if n.node_id == result.root_node_id), None)
    if root is None:
        raise DeliberationError(
            "search result is missing its root node", root_node_id=result.root_node_id
        )
    selectable = [
        node
        for node in result.nodes
        if node.parent_node_id == result.root_node_id
        and node.status is not SearchNodeStatus.FAILED
        and node.evaluation_id is not None
        and node.candidate_id is not None
    ]
    if not selectable:
        raise DeliberationError(
            "no selectable candidate in search result", root_node_id=result.root_node_id
        )
    discovery = {node.node_id: index for index, node in enumerate(result.nodes)}
    best = min(
        selectable,
        key=lambda node: (
            -(node.propagated_value if node.propagated_value is not None else 0.0),
            -(node.evaluation_score if node.evaluation_score is not None else 0.0),
            -(node.confidence if node.confidence is not None else 0.0),
            node.risk if node.risk is not None else 0.0,
            discovery[node.node_id],
        ),
    )
    return Decision(
        id=_decided_id(best),
        candidate_id=best.candidate_id or "",
        selected=True,
        rationale=(
            f"selected by deliberation search: node {best.node_id} "
            f"propagated value {best.propagated_value:.6f}"
        ),
        factors={
            "node_id": best.node_id,
            "propagated_value": best.propagated_value,
            "evaluation_score": best.evaluation_score,
            "confidence": best.confidence,
            "risk": best.risk,
            "depth": best.depth,
        },
    )


def _decided_id(node: SearchNode) -> str:
    return _derived_id(
        "dec",
        node.node_id,
        node.candidate_id,
        node.propagated_value,
        node.evaluation_score,
        node.confidence,
    )


class DeliberationSearch:
    """Bounded deterministic depth-first deliberative search (Sprint 3).

    Hard limits are module constants, never configuration — model output
    cannot change them: ``SEARCH_BRANCHING`` (B=2) valid child branches
    expanded from any node and ``SEARCH_MAX_DEPTH`` (L=2) maximum depth;
    at most 7 nodes (root + 2 children + 4 grandchildren). Plain
    depth-first traversal — no MCTS, no rollouts, no sampling.

    Predictions are HYPOTHETICAL: the input state is never mutated
    (transitions are ``model_copy``-based); branch expansion runs against
    fully re-validated hypothetical snapshots. Recording (predictions,
    evaluations, events) threads through the real state while branch
    states stay temporary and hypothetical.

    Backpropagation (deterministic, fixed): a leaf's value is its own
    ``evaluation.score``; a parent's value is the MAXIMUM propagated
    child value; a node with no valid child values falls back to its own
    ``evaluation.score``. A FAILED node has no value — never fabricated.
    """

    def __init__(
        self,
        actor: Actor,
        monitor: Monitor,
        predictor: Predictor,
        evaluator: Evaluator,
        *,
        now: Callable[[], datetime] | None = None,
        event_id: Callable[[], str] | None = None,
        events: list[BrainEvent] | None = None,
    ) -> None:
        self._actor = actor
        self._monitor = monitor
        self._predictor = predictor
        self._evaluator = evaluator
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._event_id = event_id if event_id is not None else (lambda: new_id("evt"))
        self._events = events

    def mint_event(
        self,
        state: BrainState,
        event_type: BrainEventType,
        payload: dict[str, Any],
        *,
        actor: str,
        parent_event_id: str | None,
    ) -> BrainEvent:
        event = BrainEvent(
            event_id=self._event_id(),
            mission_id=state.mission_id,
            timestamp=self._now(),
            event_type=event_type,
            actor=actor,
            parent_event_id=parent_event_id,
            payload=payload,
        )
        if self._events is not None:
            self._events.append(event)
        return event

    async def run(
        self,
        state: BrainState,
        subgoal: Subgoal,
        root_candidates: Sequence[ActionCandidate],
        *,
        parent_event_id: str | None = None,
        policy: AdaptiveSearchPolicy | None = None,
    ) -> tuple[BrainState, SearchResult]:
        if subgoal.goal_id != state.goal.id:
            raise BrainError("subgoal does not belong to the state's goal")
        # Sprint 6: optional adaptive policy. Without one, behavior is
        # byte-identical to Sprint 3 (full depth, no early termination).
        # With one, depth is still hard-capped by SEARCH_MAX_DEPTH.
        effective_max_depth = policy.max_depth if policy is not None else SEARCH_MAX_DEPTH
        # stable deterministic ordering: existing proposal order, at most B kept
        kept = list(root_candidates)[:SEARCH_BRANCHING]
        nodes: list[SearchNode] = [
            SearchNode(node_id="root", depth=0, status=SearchNodeStatus.EXPANDED)
        ]
        started_event = self.mint_event(
            state,
            BrainEventType.BRAIN_SEARCH_STARTED,
            {
                "root_node_id": "root",
                "branching": SEARCH_BRANCHING,
                "max_depth": effective_max_depth,
                "adaptive": policy is not None,
                "root_candidate_ids": [c.id for c in kept],
            },
            actor="deliberation",
            parent_event_id=parent_event_id,
        )
        state = state.with_event(started_event)
        last_event_id = started_event.event_id

        child_node_ids: list[str] = []
        child_values: list[float] = []
        for index, candidate in enumerate(kept):
            child_node_id = f"root.{index}"
            state, _node, value = await self._visit(
                state,
                state,
                subgoal,
                candidate,
                node_id=child_node_id,
                parent_node_id="root",
                depth=1,
                parent_event_id=last_event_id,
                nodes=nodes,
                max_depth=effective_max_depth,
                policy=policy,
            )
            child_node_ids.append(child_node_id)
            if value is not None:
                child_values.append(value)

        root_value = max(child_values) if child_values else None
        nodes[0] = nodes[0].model_copy(
            update={"child_node_ids": child_node_ids, "propagated_value": root_value}
        )
        if root_value is None:
            result = SearchResult(
                root_node_id="root",
                root_candidate_ids=[c.id for c in kept],
                nodes=nodes,
                status=SearchStatus.FAILED,
                failure_reason=(
                    "every root candidate failed prediction or evaluation; "
                    "no value was fabricated"
                ),
            )
        else:
            result = SearchResult(
                root_node_id="root",
                root_candidate_ids=[c.id for c in kept],
                nodes=nodes,
                status=SearchStatus.COMPLETED,
            )
        completed_event = self.mint_event(
            state,
            BrainEventType.BRAIN_SEARCH_COMPLETED,
            {
                "root_node_id": "root",
                "status": result.status.value,
                "node_count": len(nodes),
                "failure_reason": result.failure_reason[:500],
            },
            actor="deliberation",
            parent_event_id=last_event_id,
        )
        state = state.with_event(completed_event)
        state = state.model_copy(
            update={"search_trace": result, "state_version": state.state_version + 1}
        )
        return state, result

    # --------------------------------------------------------- internals ---

    async def _visit(
        self,
        real_state: BrainState,
        branch_state: BrainState,
        subgoal: Subgoal,
        candidate: ActionCandidate,
        *,
        node_id: str,
        parent_node_id: str,
        depth: int,
        parent_event_id: str | None,
        nodes: list[SearchNode],
        max_depth: int = SEARCH_MAX_DEPTH,
        policy: AdaptiveSearchPolicy | None = None,
    ) -> tuple[BrainState, SearchNode, float | None]:
        """Visit one branch: predict -> evaluate -> bounded child
        expansion (DFS).

        ``real_state`` is threaded for recording (predictions,
        evaluations, events) and is never used as a branch world;
        ``branch_state`` is the (hypothetical) world context the model
        calls see. Returns (real_state, node, propagated_value); the
        value is None when the node failed — never fabricated.

        Sprint 6: when a policy is provided, a node whose evaluation
        score AND prediction confidence both reach the policy thresholds
        is good enough — the branch terminates there (early
        termination). Expansion depth stays hard-capped (``max_depth``
        <= ``SEARCH_MAX_DEPTH``)."""
        try:
            prediction = await self._predictor.predict(branch_state, candidate, subgoal)
        except Exception as exc:  # fail closed: predictor failure -> failed node
            nodes.append(self._failed_node(node_id, parent_node_id, depth, candidate.id))
            real_state = real_state.with_event(
                self._expanded_event(
                    real_state,
                    node_id,
                    depth,
                    candidate.id,
                    SearchNodeStatus.FAILED,
                    stage="PREDICT",
                    reason=_failure_reason(exc),
                    parent_event_id=parent_event_id,
                    actor="predictor",
                )
            )
            return real_state, nodes[-1], None
        real_state = self._with_prediction(real_state, prediction)
        prediction_event = self.mint_event(
            real_state,
            BrainEventType.BRAIN_PREDICTION_PRODUCED,
            {
                "prediction_id": prediction.id,
                "candidate_id": candidate.id,
                "node_id": node_id,
                "confidence": prediction.confidence,
            },
            actor="predictor",
            parent_event_id=parent_event_id,
        )
        real_state = real_state.with_event(prediction_event)

        try:
            evaluation = await self._evaluator.evaluate(
                branch_state, candidate, prediction, subgoal
            )
        except Exception as exc:  # fail closed: evaluator failure -> failed node
            nodes.append(
                self._failed_node(
                    node_id, parent_node_id, depth, candidate.id, prediction_id=prediction.id
                )
            )
            real_state = real_state.with_event(
                self._expanded_event(
                    real_state,
                    node_id,
                    depth,
                    candidate.id,
                    SearchNodeStatus.FAILED,
                    stage="EVALUATE",
                    reason=_failure_reason(exc),
                    parent_event_id=prediction_event.event_id,
                    actor="evaluator",
                    prediction_id=prediction.id,
                )
            )
            return real_state, nodes[-1], None
        real_state = self._with_evaluation(real_state, evaluation)
        evaluation_event = self.mint_event(
            real_state,
            BrainEventType.BRAIN_EVALUATION_PRODUCED,
            {
                "evaluation_id": evaluation.id,
                "candidate_id": candidate.id,
                "node_id": node_id,
                "score": evaluation.score,
            },
            actor="evaluator",
            parent_event_id=prediction_event.event_id,
        )
        real_state = real_state.with_event(evaluation_event)

        child_node_ids: list[str] = []
        child_values: list[float] = []
        good_enough = (
            policy is not None
            and evaluation.score >= policy.stop_score
            and prediction.confidence >= policy.stop_confidence
        )
        if depth < max_depth and not good_enough:
            branch = branch_state_from_prediction(branch_state, prediction)
            children = await self._accepted_children(
                branch, subgoal, depth, evaluation_event.event_id
            )
            for index, child_candidate in enumerate(children):
                child_node_id = f"{node_id}.{index}"
                real_state, _child_node, child_value = await self._visit(
                    real_state,
                    branch,
                    subgoal,
                    child_candidate,
                    node_id=child_node_id,
                    parent_node_id=node_id,
                    depth=depth + 1,
                    parent_event_id=evaluation_event.event_id,
                    nodes=nodes,
                    max_depth=max_depth,
                    policy=policy,
                )
                child_node_ids.append(child_node_id)
                if child_value is not None:
                    child_values.append(child_value)

        if child_values:
            # backpropagation: parent value = maximum propagated child value
            value = max(child_values)
            terminal = False
        else:
            # no valid child values: the node's own evaluation.score
            value = evaluation.score
            terminal = True
        node = SearchNode(
            node_id=node_id,
            parent_node_id=parent_node_id,
            depth=depth,
            candidate_id=candidate.id,
            prediction_id=prediction.id,
            evaluation_id=evaluation.id,
            child_node_ids=child_node_ids,
            terminal=terminal,
            status=SearchNodeStatus.TERMINAL if terminal else SearchNodeStatus.EXPANDED,
            propagated_value=value,
            evaluation_score=evaluation.score,
            confidence=prediction.confidence,
            risk=evaluation.factors.risk,
        )
        nodes.append(node)
        real_state = real_state.with_event(
            self._expanded_event(
                real_state,
                node_id,
                depth,
                candidate.id,
                node.status,
                stage="EXPAND",
                reason="",
                parent_event_id=evaluation_event.event_id,
                actor="deliberation",
                prediction_id=prediction.id,
                propagated_value=value,
            )
        )
        return real_state, node, value

    async def _accepted_children(
        self,
        branch: BrainState,
        subgoal: Subgoal,
        depth: int,
        parent_event_id: str | None,
    ) -> list[ActionCandidate]:
        """Actor proposes next candidates against the HYPOTHETICAL branch
        state; the SAME monitor pipeline gates them (never bypassed); at
        most B valid children are kept (stable proposal order). An
        expansion failure yields no children — the branch stays a bounded
        terminal leaf and siblings are isolated from the failure."""
        try:
            proposal = await self._actor.propose(branch, subgoal, (), depth + 1)
            results = await self._monitor.evaluate(branch, subgoal, proposal, depth + 1)
        except Exception:
            return []
        accepted = [
            candidate
            for candidate, result in zip(proposal.candidates, results, strict=True)
            if result.decision is MonitorDecision.ACCEPTED
        ]
        return accepted[:SEARCH_BRANCHING]

    def _failed_node(
        self,
        node_id: str,
        parent_node_id: str,
        depth: int,
        candidate_id: str,
        *,
        prediction_id: str | None = None,
    ) -> SearchNode:
        return SearchNode(
            node_id=node_id,
            parent_node_id=parent_node_id,
            depth=depth,
            candidate_id=candidate_id,
            prediction_id=prediction_id,
            terminal=True,
            status=SearchNodeStatus.FAILED,
        )

    def _expanded_event(
        self,
        state: BrainState,
        node_id: str,
        depth: int,
        candidate_id: str,
        status: SearchNodeStatus,
        *,
        stage: str,
        reason: str,
        parent_event_id: str | None,
        actor: str,
        prediction_id: str | None = None,
        propagated_value: float | None = None,
    ) -> BrainEvent:
        payload: dict[str, Any] = {
            "node_id": node_id,
            "depth": depth,
            "candidate_id": candidate_id,
            "status": status.value,
            "stage": stage,
        }
        if reason:
            payload["reason"] = reason[:500]
        if prediction_id:
            payload["prediction_id"] = prediction_id
        if propagated_value is not None:
            payload["propagated_value"] = propagated_value
        return self.mint_event(
            state,
            BrainEventType.BRAIN_SEARCH_EXPANDED,
            payload,
            actor=actor,
            parent_event_id=parent_event_id,
        )

    def _with_prediction(self, state: BrainState, prediction: Prediction) -> BrainState:
        return state.model_copy(
            update={
                "predictions": [*state.predictions, prediction],
                "state_version": state.state_version + 1,
            }
        )

    def _with_evaluation(self, state: BrainState, evaluation: Evaluation) -> BrainState:
        return state.model_copy(
            update={
                "evaluations": [*state.evaluations, evaluation],
                "state_version": state.state_version + 1,
            }
        )


class DeliberationCoordinator:
    """Deliberation orchestrator (Sprint 3): monitor-gated candidates ->
    Predictor -> Evaluator -> bounded search -> deterministic selector ->
    Decision.

    No execution, no authorization, no verification: the final product is
    a Decision + ActionCandidate + Prediction/Evaluation trace — never an
    ``AuthorizedAction``. Monitor acceptance is treated as proposal-gate
    information only; the final action-selection Decision comes from the
    selector (Sprint 3 §26 semantics). Hypothetical branch states never
    become real mission state.
    """

    def __init__(
        self,
        actor: Actor,
        monitor: Monitor,
        predictor: Predictor,
        evaluator: Evaluator,
        *,
        now: Callable[[], datetime] | None = None,
        event_id: Callable[[], str] | None = None,
        events: list[BrainEvent] | None = None,
    ) -> None:
        self._search = DeliberationSearch(
            actor, monitor, predictor, evaluator, now=now, event_id=event_id, events=events
        )
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._event_id = event_id if event_id is not None else (lambda: new_id("evt"))
        self._events = events

    def mint_event(
        self,
        state: BrainState,
        event_type: BrainEventType,
        payload: dict[str, Any],
        *,
        actor: str,
        parent_event_id: str | None,
    ) -> BrainEvent:
        event = BrainEvent(
            event_id=self._event_id(),
            mission_id=state.mission_id,
            timestamp=self._now(),
            event_type=event_type,
            actor=actor,
            parent_event_id=parent_event_id,
            payload=payload,
        )
        if self._events is not None:
            self._events.append(event)
        return event

    async def deliberate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        outcome: PlanningOutcome,
        *,
        parent_event_id: str | None = None,
        policy: AdaptiveSearchPolicy | None = None,
    ) -> tuple[BrainState, DeliberationOutcome]:
        if subgoal.goal_id != state.goal.id:
            raise BrainError("subgoal does not belong to the state's goal")
        accepted = self._accepted_root_candidates(state, outcome)
        if not accepted:
            return self._fail(
                state, "no accepted root candidate to deliberate on", parent_event_id
            )
        state, result = await self._search.run(
            state, subgoal, accepted, parent_event_id=parent_event_id, policy=policy
        )
        if result.status is not SearchStatus.COMPLETED:
            return state, self._failed_outcome(result)
        try:
            decision = select_action(result)
        except DeliberationError as exc:  # fail closed: never fabricate a selection
            return state, self._failed_outcome(result, reason=_failure_reason(exc))
        selected_event = self.mint_event(
            state,
            BrainEventType.BRAIN_ACTION_SELECTED,
            {
                "candidate_id": decision.candidate_id,
                "node_id": decision.factors.get("node_id", ""),
                "propagated_value": decision.factors.get("propagated_value"),
            },
            actor="selector",
            parent_event_id=state.history[-1].event_id if state.history else None,
        )
        state = state.with_event(selected_event)
        state = state.model_copy(
            update={
                "planning_decisions": [*state.planning_decisions, decision],
                "state_version": state.state_version + 1,
            }
        )
        return state, DeliberationOutcome(
            status=SearchStatus.COMPLETED, search=result, decision=decision
        )

    # --------------------------------------------------------- internals ---

    def _accepted_root_candidates(
        self, state: BrainState, outcome: PlanningOutcome
    ) -> list[ActionCandidate]:
        """Accepted candidates in deterministic (proposal/monitor) order."""
        by_id: dict[str, ActionCandidate] = {}
        for candidate in state.action_candidates:
            by_id.setdefault(candidate.id, candidate)
        return [
            by_id[candidate_id]
            for candidate_id in outcome.accepted_candidate_ids
            if candidate_id in by_id
        ]

    @staticmethod
    def _failed_outcome(result: SearchResult, reason: str | None = None) -> DeliberationOutcome:
        return DeliberationOutcome(
            status=SearchStatus.FAILED,
            search=result,
            failure=PlanningFailure(
                stage=PlanningStage.PROPOSAL,
                reason=(reason if reason is not None else result.failure_reason)[
                    :MAX_DESCRIPTION
                ],
                iterations_used=0,
            ),
        )

    def _fail(
        self, state: BrainState, reason: str, parent_event_id: str | None
    ) -> tuple[BrainState, DeliberationOutcome]:
        failure = PlanningFailure(
            stage=PlanningStage.PROPOSAL, reason=reason[:MAX_DESCRIPTION], iterations_used=0
        )
        search = SearchResult(
            root_node_id="root", status=SearchStatus.FAILED, failure_reason=failure.reason
        )
        failure_event = self.mint_event(
            state,
            BrainEventType.BRAIN_PLANNING_FAILED,
            {
                "stage": failure.stage.value,
                "reason": failure.reason[:500],
                "iterations_used": 0,
            },
            actor="deliberation",
            parent_event_id=parent_event_id,
        )
        state = state.with_event(failure_event)
        state = state.model_copy(
            update={"search_trace": search, "state_version": state.state_version + 1}
        )
        return state, DeliberationOutcome(
            status=SearchStatus.FAILED, search=search, failure=failure
        )


class _ExplodingDecomposer:
    """Decomposer that fails if called — used when the graph already exists."""

    async def decompose(self, state: BrainState) -> None:
        raise AssertionError("decomposer must not be called when the graph already exists")
