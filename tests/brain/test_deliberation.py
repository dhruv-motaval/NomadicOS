"""NomadicBrain Sprint 3 — Deliberation test suite.

Direct deterministic tests for the five Sprint 3 components:
- ModelPredictor and prediction validation (hypothetical facts, fail-closed)
- ModelEvaluator and evaluation validation (bounds, planning estimate)
- DeliberationSearch (bounded branching/depth, backpropagation, state isolation)
- select_action (deterministic multi-key tie-breaking, fail-closed)
- DeliberationCoordinator (end-to-end deliberation flow, events, authority boundary)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import count

import pytest
from pydantic import ValidationError

from nomadicos.brain.actor import ActorProposal
from nomadicos.brain.cognition import CognitiveRequest, CognitiveRole
from nomadicos.brain.deliberation import (
    AdaptiveSearchPolicy,
    DeliberationCoordinator,
    DeliberationSearch,
    EvaluationPayload,
    ModelEvaluator,
    ModelPredictor,
    branch_state_from_prediction,
    evaluation_request_payload,
    parse_evaluation,
    parse_prediction,
    prediction_request_payload,
    select_action,
)
from nomadicos.brain.errors import (
    BrainError,
    DeliberationError,
    InvalidBrainState,
    ModelOutputRejected,
)
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.loop import (
    PlanningFailure,
    PlanningOutcome,
    PlanningStage,
    PlanningStatus,
)
from nomadicos.brain.monitor import MonitorDecision, MonitorStage
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.schemas import (
    MAX_SEARCH_NODES,
    SEARCH_BRANCHING,
    SEARCH_MAX_DEPTH,
    ActionCandidate,
    CandidateKind,
    Evaluation,
    EvaluationFactors,
    MonitorResult,
    Prediction,
    SearchNode,
    SearchNodeStatus,
    SearchResult,
    SearchStatus,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
    canonical_json,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate

from .conftest import FakeGenerator

# --------------------------------------------------------------------------- #
# Helpers & Deterministic Test Doubles
# --------------------------------------------------------------------------- #


def make_goal(goal_id: str = "task_g1") -> Goal:
    return Goal(
        id=goal_id,
        objective="Create output report artifact",
        predicates=[parse_predicate({"type": "file_exists", "path": "report.txt"})],
    )


def make_subgoal(subgoal_id: str = "sub_s1", goal_id: str = "task_g1") -> Subgoal:
    return Subgoal(id=subgoal_id, goal_id=goal_id, description="Generate report file")


def make_candidate(
    cid: str,
    subgoal_id: str = "sub_s1",
    tool: str = "terminal",
    operation: str = "write",
    revision: int = 1,
) -> ActionCandidate:
    return ActionCandidate(
        id=cid,
        kind=CandidateKind.EXECUTE,
        subgoal_id=subgoal_id,
        capability="filesystem.write",
        tool=tool,
        arguments={"path": f"{cid}.txt", "content": "data"},
        expected_effect=f"creates {cid}.txt",
        rationale=f"candidate {cid}",
        revision=revision,
    )


def make_state(
    goal_id: str = "task_g1",
    subgoal_id: str = "sub_s1",
    candidates: Sequence[ActionCandidate] = (),
    facts: dict[str, WorldFact] | None = None,
) -> BrainState:
    g = make_goal(goal_id)
    sub = make_subgoal(subgoal_id, goal_id)
    node = TaskNode(id="t1", description=sub.description, subgoal_id=sub.id)
    graph = TaskGraph.build(goal_id=g.id, nodes=[node], subgoals=[sub])
    world = WorldState(
        version=1,
        facts=facts
        if facts is not None
        else {
            "os": WorldFact(key="os", value="windows", kind=WorldFactKind.FACT),
            "dir": WorldFact(key="dir", value="/workspace", kind=WorldFactKind.FACT),
        },
    )
    s = BrainState.for_goal(g, task_graph=graph, mission_id="mission_delib_test")
    return s.model_copy(
        update={
            "world": world,
            "active_subgoal": sub,
            "action_candidates": list(candidates),
        }
    )


def active_subgoal(state: BrainState) -> Subgoal:
    assert state.active_subgoal is not None
    return state.active_subgoal


def valid_prediction_json(
    fact_key: str = "file_created",
    fact_val: str = "report.txt",
    confidence: float = 0.85,
    assumptions: list[str] | None = None,
    uncertainties: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "predicted_world": {
                "version": 1,
                "facts": {
                    fact_key: {
                        "key": fact_key,
                        "value": fact_val,
                        "kind": "PREDICTION",
                    }
                },
            },
            "confidence": confidence,
            "assumptions": assumptions if assumptions is not None else ["directory exists"],
            "uncertainties": uncertainties if uncertainties is not None else ["disk write latency"],
        }
    )


def valid_evaluation_json(
    score: float = 0.88,
    goal_progress: float = 0.85,
    feasibility: float = 0.95,
    risk: float = 0.1,
    uncertainty: float = 0.15,
    resource_cost: float = 0.05,
    reversibility: float = 0.9,
    rationale: str = "promising step toward report generation",
) -> str:
    return json.dumps(
        {
            "score": score,
            "factors": {
                "goal_progress": goal_progress,
                "feasibility": feasibility,
                "risk": risk,
                "uncertainty": uncertainty,
                "resource_cost": resource_cost,
                "reversibility": reversibility,
            },
            "rationale": rationale,
        }
    )


class ScriptedActor:
    """Actor fake: returns pre-scripted proposal batches per depth."""

    def __init__(self, batches: list[list[ActionCandidate]]) -> None:
        self.batches = list(batches)
        self.last: list[ActionCandidate] = []

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ) -> ActorProposal:
        if self.batches:
            self.last = self.batches.pop(0)
        candidates = [c.model_copy(update={"revision": iteration}) for c in self.last]
        return ActorProposal(subgoal_id=subgoal.id, iteration=iteration, candidates=candidates)


class AcceptingMonitor:
    """Monitor fake: accepts all proposed candidates structurally."""

    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal: ActorProposal,
        iteration: int,
    ) -> list[MonitorResult]:
        return [
            MonitorResult(
                candidate_id=c.id,
                decision=MonitorDecision.ACCEPTED,
                stage=MonitorStage.STRUCTURAL,
                iteration=iteration,
            )
            for c in proposal.candidates
        ]


class ScriptedPredictor:
    """Predictor test double: scripted confidence sequence."""

    def __init__(self, confidences: list[float]) -> None:
        self.confidences = list(confidences)
        self.calls = 0

    async def predict(
        self, state: BrainState, candidate: ActionCandidate, subgoal: Subgoal | None = None
    ) -> Prediction:
        self.calls += 1
        conf = self.confidences.pop(0) if len(self.confidences) > 1 else self.confidences[0]
        return Prediction(
            id=f"pred_{self.calls:03d}",
            candidate_id=candidate.id,
            predicted_world=WorldState(
                version=1,
                facts={
                    f"fact_{candidate.id}": WorldFact(
                        key=f"fact_{candidate.id}",
                        value="hypothetical_value",
                        kind=WorldFactKind.PREDICTION,
                    )
                },
            ),
            confidence=conf,
            assumptions=["scripted assumption"],
            uncertainties=["scripted uncertainty"],
        )


class ScriptedEvaluator:
    """Evaluator test double: scripted score sequence."""

    def __init__(self, scores: list[float], risks: list[float] | None = None) -> None:
        self.scores = list(scores)
        self.risks = list(risks or [0.1] * len(scores))
        self.calls = 0

    async def evaluate(
        self,
        state: BrainState,
        candidate: ActionCandidate,
        prediction: Prediction,
        subgoal: Subgoal | None = None,
    ) -> Evaluation:
        self.calls += 1
        sc = self.scores.pop(0) if len(self.scores) > 1 else self.scores[0]
        rk = self.risks.pop(0) if len(self.risks) > 1 else self.risks[0]
        return Evaluation(
            id=f"eval_{self.calls:03d}",
            candidate_id=candidate.id,
            prediction_id=prediction.id,
            score=sc,
            factors=EvaluationFactors(
                goal_progress=sc,
                feasibility=1.0,
                risk=rk,
                uncertainty=round(1.0 - sc, 4),
                resource_cost=0.05,
                reversibility=0.9,
            ),
            rationale=f"scripted score {sc}",
        )


class FailingPredictor:
    """Predictor test double that always raises an error."""

    async def predict(
        self, state: BrainState, candidate: ActionCandidate, subgoal: Subgoal | None = None
    ) -> Prediction:
        raise BrainError(f"deliberate predictor failure for {candidate.id}")


class FailingEvaluator:
    """Evaluator test double that always raises an error."""

    async def evaluate(
        self,
        state: BrainState,
        candidate: ActionCandidate,
        prediction: Prediction,
        subgoal: Subgoal | None = None,
    ) -> Evaluation:
        raise BrainError(f"deliberate evaluator failure for {candidate.id}")


# --------------------------------------------------------------------------- #
# Component A: ModelPredictor and Prediction Validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_predictor_success() -> None:
    generator = FakeGenerator()
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])
    generator.script(CognitiveRole.PREDICTOR, valid_prediction_json(confidence=0.88))

    predictor = ModelPredictor(generator)
    pred = await predictor.predict(state, c1, active_subgoal(state))

    assert pred.candidate_id == "c1"
    assert pred.confidence == 0.88
    assert "file_created" in pred.predicted_world.facts
    fact = pred.predicted_world.facts["file_created"]
    assert fact.kind is WorldFactKind.PREDICTION
    assert fact.value == "report.txt"
    assert pred.id.startswith("pred_")

    # verify generated request
    assert len(generator.calls) == 1
    req = generator.calls[0]
    assert req.role == CognitiveRole.PREDICTOR
    assert req.mission_id == state.mission_id
    assert req.payload["candidate"]["id"] == "c1"
    assert req.payload["goal_objective"] == state.goal.objective


@pytest.mark.asyncio
async def test_model_predictor_request_payload_shaping() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])
    payload = prediction_request_payload(state, c1, active_subgoal(state))

    assert payload["goal_objective"] == state.goal.objective
    assert payload["active_subgoal"] == {
        "id": active_subgoal(state).id,
        "description": active_subgoal(state).description,
    }
    assert "os" in payload["world_facts"]
    assert payload["candidate"]["id"] == "c1"
    assert isinstance(payload["capabilities"], list)
    assert isinstance(payload["constraints"], list)


@pytest.mark.asyncio
async def test_model_predictor_subgoal_mismatch_fails_closed() -> None:
    generator = FakeGenerator()
    c1 = make_candidate("c1", subgoal_id="other_subgoal")
    state = make_state()
    predictor = ModelPredictor(generator)

    with pytest.raises(InvalidBrainState, match="does not target the active subgoal"):
        await predictor.predict(state, c1, active_subgoal(state))
    assert len(generator.calls) == 0


def test_parse_prediction_rejects_non_prediction_fact_kind() -> None:
    # Model attempts to return fact with kind: "FACT" instead of "PREDICTION"
    raw_text = json.dumps(
        {
            "predicted_world": {
                "version": 1,
                "facts": {
                    "unauthorized_fact": {
                        "key": "unauthorized_fact",
                        "value": "real_data",
                        "kind": "FACT",  # ILLEGAL: hypothetical facts must be PREDICTION
                    }
                },
            },
            "confidence": 0.8,
            "assumptions": ["a1"],
            "uncertainties": ["u1"],
        }
    )
    with pytest.raises(ModelOutputRejected, match="hypothetical facts never become FACT"):
        parse_prediction(raw_text, candidate_id="c1")


@pytest.mark.parametrize(
    "invalid_text, match_str",
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2, 3]", "must be a JSON object"),
        (
            '{"confidence": 0.5}',
            "Field required",
        ),
        (
            json.dumps(
                {
                    "predicted_world": {"version": 0, "facts": {}},
                    "confidence": 1.5,  # out of bounds (> 1.0)
                }
            ),
            "less than or equal to 1",
        ),
        (
            json.dumps(
                {
                    "predicted_world": {"version": 0, "facts": {}},
                    "confidence": -0.1,  # out of bounds (< 0.0)
                }
            ),
            "greater than or equal to 0",
        ),
        (
            json.dumps(
                {
                    "predicted_world": {"version": 0, "facts": {}},
                    "confidence": 0.5,
                    "authorized": True,  # unauthorized extra field
                }
            ),
            "Extra inputs are not permitted",
        ),
        (
            json.dumps(
                {
                    "predicted_world": {"version": 0, "facts": {}},
                    "confidence": 0.5,
                    "assumptions": [""],  # empty item
                }
            ),
            "must not be empty",
        ),
        (
            json.dumps(
                {
                    "predicted_world": {"version": 0, "facts": {}},
                    "confidence": 0.5,
                    "uncertainties": ["x" * 501],  # exceeds MAX_ITEM_CHARS
                }
            ),
            "exceeds 500 chars",
        ),
    ],
)
def test_parse_prediction_fail_closed_validation(invalid_text: str, match_str: str) -> None:
    with pytest.raises(ModelOutputRejected, match=match_str):
        parse_prediction(invalid_text, candidate_id="c1")


@pytest.mark.asyncio
async def test_model_predictor_generator_exception_fails_closed() -> None:
    class ExplodingGenerator:
        async def generate_structured(self, request: CognitiveRequest) -> str:
            raise RuntimeError("Engine network timeout")

    predictor = ModelPredictor(ExplodingGenerator())
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    with pytest.raises(RuntimeError, match="Engine network timeout"):
        await predictor.predict(state, c1, active_subgoal(state))


# --------------------------------------------------------------------------- #
# Component B: ModelEvaluator and Evaluation Validation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_evaluator_success() -> None:
    generator = FakeGenerator()
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])
    generator.script(CognitiveRole.EVALUATOR, valid_evaluation_json(score=0.91, risk=0.08))

    pred = Prediction(
        id="pred_001",
        candidate_id="c1",
        predicted_world=WorldState(
            version=1,
            facts={
                "f1": WorldFact(key="f1", value="v1", kind=WorldFactKind.PREDICTION),
            },
        ),
        confidence=0.88,
    )

    evaluator = ModelEvaluator(generator)
    ev = await evaluator.evaluate(state, c1, pred, active_subgoal(state))

    assert ev.candidate_id == "c1"
    assert ev.prediction_id == "pred_001"
    assert ev.score == 0.91
    assert ev.factors.risk == 0.08
    assert ev.factors.goal_progress == 0.85
    assert ev.id.startswith("eval_")

    # verify generated request
    assert len(generator.calls) == 1
    req = generator.calls[0]
    assert req.role == CognitiveRole.EVALUATOR
    assert req.mission_id == state.mission_id
    assert req.payload["candidate"]["id"] == "c1"
    assert req.payload["prediction"]["confidence"] == 0.88


@pytest.mark.asyncio
async def test_model_evaluator_request_payload_shaping() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])
    pred = Prediction(
        id="pred_001",
        candidate_id="c1",
        predicted_world=WorldState(
            version=1,
            facts={"f1": WorldFact(key="f1", value="v1", kind=WorldFactKind.PREDICTION)},
        ),
        confidence=0.75,
        assumptions=["a1"],
        uncertainties=["u1"],
    )
    payload = evaluation_request_payload(state, c1, pred, active_subgoal(state))

    assert payload["goal_objective"] == state.goal.objective
    assert payload["candidate"]["id"] == "c1"
    assert payload["prediction"]["confidence"] == 0.75
    assert "f1" in payload["prediction"]["predicted_facts"]
    assert payload["prediction"]["assumptions"] == ["a1"]


@pytest.mark.parametrize(
    "score, factors, match_str",
    [
        (
            -0.1,
            {
                "goal_progress": 0.5,
                "feasibility": 0.5,
                "risk": 0.5,
                "uncertainty": 0.5,
                "resource_cost": 0.5,
                "reversibility": 0.5,
            },
            "greater than or equal to 0",
        ),
        (
            1.2,
            {
                "goal_progress": 0.5,
                "feasibility": 0.5,
                "risk": 0.5,
                "uncertainty": 0.5,
                "resource_cost": 0.5,
                "reversibility": 0.5,
            },
            "less than or equal to 1",
        ),
        (
            0.8,
            {
                "goal_progress": 1.5,
                "feasibility": 0.5,
                "risk": 0.5,
                "uncertainty": 0.5,
                "resource_cost": 0.5,
                "reversibility": 0.5,
            },
            "less than or equal to 1",
        ),
        (
            0.8,
            {
                "goal_progress": 0.5,
                "feasibility": -0.2,
                "risk": 0.5,
                "uncertainty": 0.5,
                "resource_cost": 0.5,
                "reversibility": 0.5,
            },
            "greater than or equal to 0",
        ),
    ],
)
def test_model_evaluator_score_and_factor_bounds(
    score: float, factors: dict[str, float], match_str: str
) -> None:
    raw_text = json.dumps({"score": score, "factors": factors, "rationale": "test"})
    with pytest.raises(ModelOutputRejected, match=match_str):
        parse_evaluation(raw_text, candidate_id="c1", prediction_id="p1")


@pytest.mark.parametrize(
    "invalid_text, match_str",
    [
        ("corrupted non-json", "not valid JSON"),
        ('{"score": 0.8}', "Field required"),  # missing factors
        (
            json.dumps(
                {
                    "score": 0.8,
                    "factors": {
                        "goal_progress": 0.8,
                        "feasibility": 0.8,
                        "risk": 0.1,
                        "uncertainty": 0.1,
                        "resource_cost": 0.1,
                        "reversibility": 0.9,
                    },
                    "authorized": True,  # unauthorized field
                }
            ),
            "Extra inputs are not permitted",
        ),
        (
            json.dumps(
                {
                    "score": 0.8,
                    "factors": {
                        "goal_progress": 0.8,
                        "feasibility": 0.8,
                        "risk": 0.1,
                        "uncertainty": 0.1,
                        "resource_cost": 0.1,
                        "reversibility": 0.9,
                    },
                    "rationale": "r" * 2001,  # exceeds MAX_DESCRIPTION
                }
            ),
            "exceeds 2000 chars",
        ),
    ],
)
def test_parse_evaluation_fail_closed_validation(invalid_text: str, match_str: str) -> None:
    with pytest.raises(ModelOutputRejected, match=match_str):
        parse_evaluation(invalid_text, candidate_id="c1", prediction_id="p1")


def test_evaluation_is_planning_estimate_never_authorization() -> None:
    raw_text = valid_evaluation_json(score=0.99)
    ev = parse_evaluation(raw_text, candidate_id="c1", prediction_id="p1")
    # Contract is frozen data only; cannot mutate or inject authorization tokens
    assert ev.score == 0.99
    assert not hasattr(ev, "authorized")
    assert not hasattr(ev, "approved")
    with pytest.raises(ValidationError):
        EvaluationPayload.model_validate(
            {"score": 0.99, "factors": ev.factors.model_dump(), "authorized": True}
        )


# --------------------------------------------------------------------------- #
# Component C: DeliberationSearch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deliberation_search_success_and_node_count() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    actor = ScriptedActor(
        [
            [make_candidate("c1_0"), make_candidate("c1_1")],
            [make_candidate("c2_0"), make_candidate("c2_1")],
        ]
    )
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8, 0.85, 0.9, 0.75, 0.7, 0.65])
    evaluator = ScriptedEvaluator([0.6, 0.9, 0.8, 0.5, 0.85, 0.4])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    next_state, result = await search.run(state, active_subgoal(state), [c1, c2])

    assert result.status is SearchStatus.COMPLETED
    assert result.root_node_id == "root"
    assert len(result.root_candidate_ids) == 2
    # B=2, L=2 -> root(1) + 2 children + 4 grandchildren = 7 nodes total
    assert len(result.nodes) == MAX_SEARCH_NODES
    assert len(result.nodes) <= 7

    # verify all node depths are <= SEARCH_MAX_DEPTH
    for node in result.nodes:
        assert node.depth <= SEARCH_MAX_DEPTH

    # check recording into state
    assert len(next_state.predictions) == 6
    assert len(next_state.evaluations) == 6
    assert next_state.search_trace == result


@pytest.mark.asyncio
async def test_deliberation_search_bounds_branching() -> None:
    # Supply 5 root candidates -> search MUST keep at most SEARCH_BRANCHING (2)
    candidates = [make_candidate(f"c{i}") for i in range(5)]
    state = make_state(candidates=candidates)

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8])
    evaluator = ScriptedEvaluator([0.7])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), candidates)

    assert len(result.root_candidate_ids) == SEARCH_BRANCHING
    assert result.root_candidate_ids == ["c0", "c1"]
    root_node = next(n for n in result.nodes if n.node_id == "root")
    assert len(root_node.child_node_ids) == SEARCH_BRANCHING


@pytest.mark.asyncio
async def test_deliberation_search_deterministic_backpropagation() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    # root.0 children: c1_0 (score 0.7), c1_1 (score 0.9) -> root.0 value = max(0.7, 0.9) = 0.9
    # root.1 children: c2_0 (score 0.4), c2_1 (score 0.6) -> root.1 value = max(0.4, 0.6) = 0.6
    # root value = max(0.9, 0.6) = 0.9
    actor = ScriptedActor(
        [
            [make_candidate("c1_0"), make_candidate("c1_1")],
            [make_candidate("c2_0"), make_candidate("c2_1")],
        ]
    )
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.85] * 6)
    evaluator = ScriptedEvaluator([0.5, 0.7, 0.9, 0.5, 0.4, 0.6])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), [c1, c2])

    nodes_by_id = {n.node_id: n for n in result.nodes}
    assert nodes_by_id["root.0.0"].propagated_value == 0.7
    assert nodes_by_id["root.0.1"].propagated_value == 0.9
    assert nodes_by_id["root.0"].propagated_value == 0.9

    assert nodes_by_id["root.1.0"].propagated_value == 0.4
    assert nodes_by_id["root.1.1"].propagated_value == 0.6
    assert nodes_by_id["root.1"].propagated_value == 0.6

    assert nodes_by_id["root"].propagated_value == 0.9


@pytest.mark.asyncio
async def test_deliberation_search_predictor_failure_creates_failed_node() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    class SemiFailingPredictor:
        async def predict(
            self, state: BrainState, candidate: ActionCandidate, subgoal=None
        ) -> Prediction:
            if candidate.id == "c1":
                raise BrainError("c1 prediction failed")
            return Prediction(
                id="pred_c2",
                candidate_id=candidate.id,
                predicted_world=WorldState(
                    version=1,
                    facts={"f": WorldFact(key="f", value="ok", kind=WorldFactKind.PREDICTION)},
                ),
                confidence=0.8,
            )

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = SemiFailingPredictor()
    evaluator = ScriptedEvaluator([0.75])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), [c1, c2])

    assert result.status is SearchStatus.COMPLETED
    nodes = {n.node_id: n for n in result.nodes}
    # root.0 failed
    assert nodes["root.0"].status is SearchNodeStatus.FAILED
    assert nodes["root.0"].terminal is True
    assert nodes["root.0"].propagated_value is None
    # root.1 succeeded
    assert nodes["root.1"].status is SearchNodeStatus.TERMINAL
    assert nodes["root.1"].propagated_value == 0.75
    assert nodes["root"].propagated_value == 0.75


@pytest.mark.asyncio
async def test_deliberation_search_evaluator_failure_creates_failed_node() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    class SemiFailingEvaluator:
        async def evaluate(
            self, state: BrainState, candidate: ActionCandidate, prediction, subgoal=None
        ) -> Evaluation:
            if candidate.id == "c1":
                raise BrainError("c1 evaluation failed")
            return Evaluation(
                id="eval_c2",
                candidate_id=candidate.id,
                prediction_id=prediction.id,
                score=0.85,
                factors=EvaluationFactors(
                    goal_progress=0.85,
                    feasibility=1.0,
                    risk=0.1,
                    uncertainty=0.1,
                    resource_cost=0.1,
                    reversibility=1.0,
                ),
            )

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8, 0.8])
    evaluator = SemiFailingEvaluator()

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), [c1, c2])

    nodes = {n.node_id: n for n in result.nodes}
    assert nodes["root.0"].status is SearchNodeStatus.FAILED
    assert nodes["root.0"].terminal is True
    assert nodes["root.0"].propagated_value is None
    assert nodes["root.1"].propagated_value == 0.85


@pytest.mark.asyncio
async def test_deliberation_search_all_candidates_fail_fails_search_closed() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = FailingPredictor()
    evaluator = ScriptedEvaluator([0.5])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), [c1])

    assert result.status is SearchStatus.FAILED
    assert "every root candidate failed prediction or evaluation" in result.failure_reason
    root = next(n for n in result.nodes if n.node_id == "root")
    assert root.propagated_value is None


@pytest.mark.asyncio
async def test_deliberation_search_hypothetical_state_isolation() -> None:
    c1 = make_candidate("c1")
    initial_facts = {
        "os": WorldFact(key="os", value="windows", kind=WorldFactKind.FACT),
        "status": WorldFact(key="status", value="clean", kind=WorldFactKind.FACT),
    }
    state = make_state(candidates=[c1], facts=initial_facts)

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.9])
    evaluator = ScriptedEvaluator([0.85])

    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    next_state, _result = await search.run(state, active_subgoal(state), [c1])

    # Caller's original state.world must be strictly identical and unmutated
    assert state.world.facts["status"].value == "clean"
    assert next_state.world.facts["status"].value == "clean"
    assert "fact_c1" not in next_state.world.facts  # prediction fact did not leak into real world
    assert next_state.state_version > state.state_version


def test_branch_state_from_prediction_hypothetical_snapshot() -> None:
    state = make_state()
    pred = Prediction(
        id="pred_h1",
        candidate_id="c1",
        predicted_world=WorldState(
            version=9,
            facts={
                "pred_key": WorldFact(key="pred_key", value="hypo", kind=WorldFactKind.PREDICTION)
            },
        ),
        confidence=0.88,
    )
    branch = branch_state_from_prediction(state, pred)

    assert branch.state_version == state.state_version + 1
    assert "pred_key" in branch.world.facts
    assert branch.world.facts["pred_key"].kind is WorldFactKind.PREDICTION
    # Original state is untouched
    assert "pred_key" not in state.world.facts


@pytest.mark.asyncio
async def test_deliberation_search_deterministic_replay() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    def fixed_time() -> datetime:
        return datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)

    async def run_one_search() -> SearchResult:
        cnt = count(1)

        def event_id() -> str:
            return f"evt_{next(cnt):03d}"

        actor = ScriptedActor(
            [
                [make_candidate("c1_0"), make_candidate("c1_1")],
                [make_candidate("c2_0"), make_candidate("c2_1")],
            ]
        )
        monitor = AcceptingMonitor()
        predictor = ScriptedPredictor([0.8, 0.85, 0.9, 0.75, 0.7, 0.65])
        evaluator = ScriptedEvaluator([0.6, 0.9, 0.8, 0.5, 0.85, 0.4])
        search = DeliberationSearch(
            actor, monitor, predictor, evaluator, now=fixed_time, event_id=event_id
        )
        _st, res = await search.run(state, active_subgoal(state), [c1, c2])
        return res

    res1 = await run_one_search()
    res2 = await run_one_search()
    assert canonical_json(res1) == canonical_json(res2)


def test_adaptive_search_policy_max_depth_hard_bounded() -> None:
    # Cannot exceed SEARCH_MAX_DEPTH
    with pytest.raises(ValidationError):
        AdaptiveSearchPolicy(max_depth=SEARCH_MAX_DEPTH + 1)


@pytest.mark.asyncio
async def test_adaptive_search_policy_early_termination() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    # With stop_score=0.9 and stop_confidence=0.9,
    # depth 1 node with score 0.95 & conf 0.95 terminates early
    actor = ScriptedActor([[make_candidate("c1_deeper")]])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.95])
    evaluator = ScriptedEvaluator([0.95])

    policy = AdaptiveSearchPolicy(stop_score=0.9, stop_confidence=0.9)
    search = DeliberationSearch(actor, monitor, predictor, evaluator)
    _state, result = await search.run(state, active_subgoal(state), [c1], policy=policy)

    assert result.status is SearchStatus.COMPLETED
    # Early termination: root + 1 child = 2 nodes; grandchild was not expanded
    assert len(result.nodes) == 2
    child = next(n for n in result.nodes if n.node_id == "root.0")
    assert child.child_node_ids == []
    assert child.terminal is True


# --------------------------------------------------------------------------- #
# Component D: select_action and Deterministic Selection
# --------------------------------------------------------------------------- #


def _make_search_result(children: list[SearchNode]) -> SearchResult:
    root = SearchNode(
        node_id="root",
        depth=0,
        status=SearchNodeStatus.EXPANDED,
        child_node_ids=[c.node_id for c in children],
        propagated_value=max(
            (c.propagated_value for c in children if c.propagated_value is not None), default=None
        ),
    )
    return SearchResult(
        root_node_id="root",
        root_candidate_ids=[c.candidate_id for c in children if c.candidate_id is not None],
        nodes=[root, *children],
        status=SearchStatus.COMPLETED,
    )


def test_select_action_picks_highest_propagated_value() -> None:
    child0 = SearchNode(
        node_id="root.0",
        parent_node_id="root",
        depth=1,
        candidate_id="c0",
        prediction_id="p0",
        evaluation_id="e0",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.75,
        evaluation_score=0.75,
        confidence=0.8,
        risk=0.1,
    )
    child1 = SearchNode(
        node_id="root.1",
        parent_node_id="root",
        depth=1,
        candidate_id="c1",
        prediction_id="p1",
        evaluation_id="e1",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.92,
        evaluation_score=0.92,
        confidence=0.8,
        risk=0.1,
    )
    result = _make_search_result([child0, child1])
    decision = select_action(result)

    assert decision.selected is True
    assert decision.candidate_id == "c1"
    assert decision.factors["node_id"] == "root.1"
    assert decision.factors["propagated_value"] == 0.92


def test_select_action_tie_breaking_ladder() -> None:
    # 1. Tied propagated value -> higher own evaluation_score wins
    c0 = SearchNode(
        node_id="root.0",
        parent_node_id="root",
        depth=1,
        candidate_id="c0",
        prediction_id="p0",
        evaluation_id="e0",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.8,
        evaluation_score=0.7,
        confidence=0.8,
        risk=0.2,
    )
    c1 = SearchNode(
        node_id="root.1",
        parent_node_id="root",
        depth=1,
        candidate_id="c1",
        prediction_id="p1",
        evaluation_id="e1",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.8,
        evaluation_score=0.75,
        confidence=0.8,
        risk=0.2,
    )
    assert select_action(_make_search_result([c0, c1])).candidate_id == "c1"

    # 2. Tied propagated value & evaluation_score -> higher confidence wins
    c0_b = c0.model_copy(update={"evaluation_score": 0.75, "confidence": 0.85})
    c1_b = c1.model_copy(update={"evaluation_score": 0.75, "confidence": 0.80})
    assert select_action(_make_search_result([c0_b, c1_b])).candidate_id == "c0"

    # 3. Tied propagated, eval_score, confidence -> lower risk wins
    c0_c = c0_b.model_copy(update={"confidence": 0.85, "risk": 0.05})
    c1_c = c1_b.model_copy(update={"confidence": 0.85, "risk": 0.20})
    assert select_action(_make_search_result([c0_c, c1_c])).candidate_id == "c0"

    # 4. Tied all 4 metrics -> stable discovery order (root.0 before root.1)
    c0_d = c0_c.model_copy(update={"risk": 0.1})
    c1_d = c1_c.model_copy(update={"risk": 0.1})
    assert select_action(_make_search_result([c0_d, c1_d])).candidate_id == "c0"


def test_select_action_excludes_failed_and_unevaluated_nodes() -> None:
    failed_child = SearchNode(
        node_id="root.0",
        parent_node_id="root",
        depth=1,
        candidate_id="c0",
        status=SearchNodeStatus.FAILED,
        terminal=True,
    )
    valid_child = SearchNode(
        node_id="root.1",
        parent_node_id="root",
        depth=1,
        candidate_id="c1",
        prediction_id="p1",
        evaluation_id="e1",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.6,
        evaluation_score=0.6,
        confidence=0.7,
        risk=0.1,
    )
    result = _make_search_result([failed_child, valid_child])
    decision = select_action(result)
    assert decision.candidate_id == "c1"


def test_select_action_excludes_grandchild_nodes() -> None:
    # A grandchild node with a very high propagated value cannot be selected directly
    root = SearchNode(
        node_id="root", depth=0, status=SearchNodeStatus.EXPANDED, child_node_ids=["root.0"]
    )
    direct_child = SearchNode(
        node_id="root.0",
        parent_node_id="root",
        depth=1,
        candidate_id="c_child",
        prediction_id="p0",
        evaluation_id="e0",
        status=SearchNodeStatus.EXPANDED,
        child_node_ids=["root.0.0"],
        propagated_value=0.99,
        evaluation_score=0.5,
        confidence=0.8,
        risk=0.1,
    )
    grandchild = SearchNode(
        node_id="root.0.0",
        parent_node_id="root.0",
        depth=2,
        candidate_id="c_grandchild",
        prediction_id="p0_0",
        evaluation_id="e0_0",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.99,
        evaluation_score=0.99,
        confidence=0.99,
        risk=0.01,
    )
    result = SearchResult(
        root_node_id="root",
        root_candidate_ids=["c_child"],
        nodes=[root, direct_child, grandchild],
        status=SearchStatus.COMPLETED,
    )
    decision = select_action(result)
    # MUST select direct child c_child, never c_grandchild
    assert decision.candidate_id == "c_child"
    assert decision.factors["node_id"] == "root.0"


def test_select_action_deterministic_ids() -> None:
    child0 = SearchNode(
        node_id="root.0",
        parent_node_id="root",
        depth=1,
        candidate_id="c0",
        prediction_id="p0",
        evaluation_id="e0",
        status=SearchNodeStatus.TERMINAL,
        propagated_value=0.88,
        evaluation_score=0.88,
        confidence=0.9,
        risk=0.1,
    )
    res = _make_search_result([child0])
    d1 = select_action(res)
    d2 = select_action(res)
    assert d1.id == d2.id
    assert d1.candidate_id == d2.candidate_id == "c0"


def test_select_action_fails_closed_on_invalid_results() -> None:
    # 1. Uncompleted status
    failed_res = SearchResult(
        root_node_id="root", status=SearchStatus.FAILED, failure_reason="failed"
    )
    with pytest.raises(DeliberationError, match="cannot select from a FAILED"):
        select_action(failed_res)

    # 2. Missing root node
    no_root_res = SearchResult(root_node_id="root", status=SearchStatus.COMPLETED, nodes=[])
    with pytest.raises(DeliberationError, match="missing its root node"):
        select_action(no_root_res)

    # 3. No selectable candidate
    root_only = SearchNode(node_id="root", depth=0, status=SearchNodeStatus.EXPANDED)
    empty_res = SearchResult(root_node_id="root", status=SearchStatus.COMPLETED, nodes=[root_only])
    with pytest.raises(DeliberationError, match="no selectable candidate"):
        select_action(empty_res)


# --------------------------------------------------------------------------- #
# Component E: DeliberationCoordinator
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coordinator_success_flow() -> None:
    c1 = make_candidate("c1")
    c2 = make_candidate("c2")
    state = make_state(candidates=[c1, c2])

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8, 0.9])
    evaluator = ScriptedEvaluator([0.65, 0.88])

    events: list[BrainEvent] = []
    coordinator = DeliberationCoordinator(actor, monitor, predictor, evaluator, events=events)

    outcome = PlanningOutcome(
        status=PlanningStatus.ACCEPTED,
        iterations_used=1,
        accepted_candidate_ids=["c1", "c2"],
        monitor_results=[
            MonitorResult(
                candidate_id="c1", decision=MonitorDecision.ACCEPTED, stage=MonitorStage.STRUCTURAL
            ),
            MonitorResult(
                candidate_id="c2", decision=MonitorDecision.ACCEPTED, stage=MonitorStage.STRUCTURAL
            ),
        ],
    )

    next_state, delib_outcome = await coordinator.deliberate(state, active_subgoal(state), outcome)

    assert delib_outcome.status is SearchStatus.COMPLETED
    assert delib_outcome.decision is not None
    assert delib_outcome.decision.selected is True
    assert delib_outcome.decision.candidate_id == "c2"  # higher score 0.88
    assert delib_outcome.failure is None

    # verify event provenance
    action_selected_events = [
        e for e in events if e.event_type == BrainEventType.BRAIN_ACTION_SELECTED
    ]
    assert len(action_selected_events) == 1
    assert action_selected_events[0].payload["candidate_id"] == "c2"
    assert next_state.planning_decisions[-1] == delib_outcome.decision
    assert any(ref.event_id == action_selected_events[0].event_id for ref in next_state.history)


@pytest.mark.asyncio
async def test_coordinator_empty_accepted_candidates_fails_closed() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8])
    evaluator = ScriptedEvaluator([0.7])

    events: list[BrainEvent] = []
    coordinator = DeliberationCoordinator(actor, monitor, predictor, evaluator, events=events)

    # Empty accepted list in outcome
    outcome = PlanningOutcome(
        status=PlanningStatus.FAILED,
        iterations_used=1,
        failure=PlanningFailure(
            stage=PlanningStage.PROPOSAL, reason="all rejected", iterations_used=1
        ),
    )

    next_state, delib_outcome = await coordinator.deliberate(state, active_subgoal(state), outcome)

    assert delib_outcome.status is SearchStatus.FAILED
    assert delib_outcome.decision is None
    assert delib_outcome.failure is not None
    assert "no accepted root candidate" in delib_outcome.failure.reason

    failed_events = [e for e in events if e.event_type == BrainEventType.BRAIN_PLANNING_FAILED]
    assert len(failed_events) == 1
    assert any(ref.event_id == failed_events[0].event_id for ref in next_state.history)


@pytest.mark.asyncio
async def test_coordinator_subgoal_mismatch_raises() -> None:
    state = make_state()
    foreign_subgoal = make_subgoal("s_foreign", goal_id="different_goal")

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.8])
    evaluator = ScriptedEvaluator([0.7])
    coordinator = DeliberationCoordinator(actor, monitor, predictor, evaluator)

    outcome = PlanningOutcome(
        status=PlanningStatus.ACCEPTED,
        iterations_used=1,
        accepted_candidate_ids=["c1"],
    )

    with pytest.raises(BrainError, match="subgoal does not belong to the state's goal"):
        await coordinator.deliberate(state, foreign_subgoal, outcome)


@pytest.mark.asyncio
async def test_coordinator_search_failure_fails_outcome() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = FailingPredictor()
    evaluator = ScriptedEvaluator([0.7])

    coordinator = DeliberationCoordinator(actor, monitor, predictor, evaluator)
    outcome = PlanningOutcome(
        status=PlanningStatus.ACCEPTED,
        iterations_used=1,
        accepted_candidate_ids=["c1"],
    )

    next_state, delib_outcome = await coordinator.deliberate(state, active_subgoal(state), outcome)

    assert delib_outcome.status is SearchStatus.FAILED
    assert delib_outcome.decision is None
    assert delib_outcome.failure is not None
    assert "every root candidate failed" in delib_outcome.failure.reason


@pytest.mark.asyncio
async def test_coordinator_never_authorizes_or_executes() -> None:
    c1 = make_candidate("c1")
    state = make_state(candidates=[c1])

    actor = ScriptedActor([])
    monitor = AcceptingMonitor()
    predictor = ScriptedPredictor([0.88])
    evaluator = ScriptedEvaluator([0.92])

    coordinator = DeliberationCoordinator(actor, monitor, predictor, evaluator)
    outcome = PlanningOutcome(
        status=PlanningStatus.ACCEPTED,
        iterations_used=1,
        accepted_candidate_ids=["c1"],
    )

    next_state, delib_outcome = await coordinator.deliberate(state, active_subgoal(state), outcome)

    # Decision means "selected by deliberation" - never an AuthorizedAction
    assert delib_outcome.decision is not None
    assert delib_outcome.decision.selected is True
    assert not hasattr(delib_outcome.decision, "authorized")
    assert not hasattr(delib_outcome.decision, "execute")

    # Real world state is not modified
    assert next_state.world == state.world
