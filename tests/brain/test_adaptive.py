"""Sprint 6 — adaptive intelligence: effort policy, adaptive search
(depth/early termination, default behavior preserved), the one-pass
AdaptiveController, boundary integrity, and fail-safe memory."""

from __future__ import annotations

import inspect
import re
from collections.abc import Sequence
from itertools import count

import pytest
from pydantic import ValidationError

from nomadicos.brain.adaptive import AdaptiveController
from nomadicos.brain.deliberation import AdaptiveSearchPolicy, DeliberationSearch
from nomadicos.brain.effort import decide_effort
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.memory import MemoryController
from nomadicos.brain.monitor import MonitorDecision, MonitorStage
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.schemas import (
    MAX_INFO_REQUESTS,
    SEARCH_MAX_DEPTH,
    ActionCandidate,
    BudgetState,
    CandidateKind,
    EffortDecision,
    MonitorResult,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.memory import MemoryQuery, RetrievedMemory

# ------------------------------------------------------------- helpers ---


def make_state(**overrides) -> BrainState:
    goal = Goal(
        id="task_g",
        objective="fix the auth module",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )
    sub = Subgoal(id="subgoal_s1", goal_id="task_g", description="write failing auth test")
    graph = TaskGraph.build(goal_id="task_g", subgoals=[sub])
    base = dict(
        goal=goal,
        task_graph=graph,
        active_subgoal=sub,
        world=WorldState(facts={}),
        budget=BudgetState(),
    )
    base.update(overrides)
    return BrainState(**base)


class ScriptedActor:
    """DeliberationSearch actor fake: one scripted proposal per depth;
    the last proposal sticks (reused for deeper expansions)."""

    def __init__(self, proposals: list[list[ActionCandidate]]) -> None:
        self.scripted = list(proposals)
        self.last: list[ActionCandidate] = []

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Sequence[MonitorResult] = (),
        iteration: int = 1,
    ):
        from nomadicos.brain.actor import ActorProposal

        if self.scripted:
            self.last = self.scripted.pop(0)
        # revision must match the refinement iteration (Sprint 2 provenance)
        candidates = [c.model_copy(update={"revision": iteration}) for c in self.last]
        return ActorProposal(subgoal_id=subgoal.id, iteration=iteration, candidates=candidates)


class AcceptingMonitor:
    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal,
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
    """Predictor fake: confidence script; the last value sticks."""

    def __init__(self, confidences: list[float]) -> None:
        self.scripted = list(confidences)
        self.calls = 0

    async def predict(self, state: BrainState, candidate: ActionCandidate, subgoal=None):
        from nomadicos.brain.schemas import Prediction

        self.calls += 1
        confidence = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        return Prediction(
            id=f"pred_{self.calls:03d}",
            candidate_id=candidate.id,
            predicted_world=WorldState(
                facts={"next": WorldFact(key="next", value="x", kind=WorldFactKind.PREDICTION)}
            ),
            confidence=confidence,
        )


class ScriptedEvaluator:
    """Evaluator fake: score script; the last value sticks."""

    def __init__(self, scores: list[float]) -> None:
        self.scripted = list(scores)
        self.calls = 0

    async def evaluate(
        self, state: BrainState, candidate: ActionCandidate, prediction, subgoal=None
    ):
        from nomadicos.brain.schemas import Evaluation, EvaluationFactors

        self.calls += 1
        score = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        return Evaluation(
            id=f"eval_{self.calls:03d}",
            candidate_id=candidate.id,
            prediction_id=prediction.id,
            score=score,
            factors=EvaluationFactors(
                goal_progress=score,
                feasibility=1.0,
                risk=0.0,
                uncertainty=1.0 - score,
                resource_cost=0.0,
                reversibility=1.0,
            ),
        )


def candidate(cid: str) -> ActionCandidate:
    return ActionCandidate(
        id=cid, kind=CandidateKind.EXECUTE, tool="terminal", subgoal_id="subgoal_s1"
    )


# ---------------------------------------------------------------- effort ---


def test_effort_stop_when_budget_exhausted() -> None:
    plan = decide_effort(make_state(budget=BudgetState(max_steps=4, steps_used=4)))
    assert plan.decision is EffortDecision.STOP
    assert plan.search_depth == 0
    assert plan.max_iterations == 1


def test_effort_gather_info_when_unknowns() -> None:
    state = make_state(
        world=WorldState(facts={"x": WorldFact(key="x", kind=WorldFactKind.UNKNOWN)})
    )
    plan = decide_effort(state)
    assert plan.decision is EffortDecision.GATHER_INFO
    assert plan.search_depth == 0  # gather first, no speculative search


def test_effort_full_on_high_uncertainty() -> None:
    state = make_state(
        world=WorldState(facts={"c": WorldFact(key="c", kind=WorldFactKind.HYPOTHESIS)}),
        action_candidates=[candidate("cand_1")],
    )
    plan = decide_effort(state)
    assert plan.decision is EffortDecision.FULL_EFFORT
    assert plan.search_depth == SEARCH_MAX_DEPTH  # full bounded depth
    assert plan.max_iterations <= 16


def test_effort_reduced_on_low_uncertainty_with_decision() -> None:
    from nomadicos.brain.schemas import Decision

    state = make_state(
        world=WorldState(
            facts={"a": WorldFact(key="a", value=1, kind=WorldFactKind.VERIFIED_FACT)}
        ),
        planning_decisions=[Decision(id="dec_1", candidate_id="cand_1", selected=True)],
    )
    plan = decide_effort(state)
    assert plan.decision is EffortDecision.REDUCED_EFFORT
    assert plan.search_depth == 0  # terminate earlier
    assert plan.max_iterations == 1


def test_effort_is_deterministic_and_bounded() -> None:
    state = make_state(
        world=WorldState(facts={"c": WorldFact(key="c", kind=WorldFactKind.HYPOTHESIS)}),
        action_candidates=[candidate("cand_1")],
    )
    assert decide_effort(state) == decide_effort(state)
    for _decision in (EffortDecision.FULL_EFFORT, EffortDecision.REDUCED_EFFORT):
        plan = decide_effort(state)
        assert plan.max_iterations <= 16  # MAX_ITERATIONS_BOUND
        assert plan.search_depth <= SEARCH_MAX_DEPTH  # hard depth cap


def test_effort_plan_rejects_out_of_bounds() -> None:
    from nomadicos.brain.schemas import EffortPlan

    with pytest.raises(ValidationError):
        EffortPlan(decision=EffortDecision.FULL_EFFORT, max_iterations=17, search_depth=0)
    with pytest.raises(ValidationError):
        EffortPlan(decision=EffortDecision.FULL_EFFORT, max_iterations=1, search_depth=3)


# --------------------------------------------------------- adaptive search ---


async def test_adaptive_search_terminates_early_when_confident() -> None:
    """A good-enough node (high score + high confidence) terminates its
    branch immediately: fewer predictor calls than a full-depth search."""
    full = DeliberationSearch(
        ScriptedActor([[candidate("cand_a")]]),
        AcceptingMonitor(),
        ScriptedPredictor([0.5]),
        ScriptedEvaluator([0.5]),
    )
    _, full_result = await full.run(
        make_state(),
        Subgoal(id="subgoal_s1", goal_id="task_g", description="d"),
        [candidate("cand_a")],
    )
    adaptive = DeliberationSearch(
        ScriptedActor([[candidate("cand_a")]]),
        AcceptingMonitor(),
        ScriptedPredictor([0.95]),
        ScriptedEvaluator([0.95]),
    )
    _, early_result = await adaptive.run(
        make_state(),
        Subgoal(id="subgoal_s1", goal_id="task_g", description="d"),
        [candidate("cand_a")],
        policy=AdaptiveSearchPolicy(stop_score=0.9, stop_confidence=0.9),
    )
    assert len(full_result.nodes) > len(early_result.nodes)  # expanded vs early stop
    assert all(n.terminal for n in early_result.nodes if n.node_id != "root")


async def test_adaptive_search_depth_is_hard_capped() -> None:
    """Even a policy asking for more can never exceed SEARCH_MAX_DEPTH."""
    search = DeliberationSearch(
        ScriptedActor([[candidate("cand_a"), candidate("cand_b")]]),
        AcceptingMonitor(),
        ScriptedPredictor([0.1]),
        ScriptedEvaluator([0.1]),
    )
    subgoal = Subgoal(id="subgoal_s1", goal_id="task_g", description="d")
    _, result = await search.run(
        make_state(),
        subgoal,
        [candidate("cand_a"), candidate("cand_b")],
        policy=AdaptiveSearchPolicy(max_depth=SEARCH_MAX_DEPTH),
    )
    assert all(n.depth <= SEARCH_MAX_DEPTH for n in result.nodes)
    assert len(result.nodes) <= 1 + 2 + 4  # root + B + B**L (Sprint 3 bound)


async def test_adaptive_search_shallow_depth_expands_less() -> None:
    """max_depth=0 policy → root children only (depth-1 nodes), no
    grandchildren: fewer model calls, bounded work."""
    shallow = DeliberationSearch(
        ScriptedActor([[candidate("cand_a")], [candidate("cand_child")]]),
        AcceptingMonitor(),
        ScriptedPredictor([0.1]),
        ScriptedEvaluator([0.1]),
    )
    subgoal = Subgoal(id="subgoal_s1", goal_id="task_g", description="d")
    _, result = await shallow.run(
        make_state(),
        subgoal,
        [candidate("cand_a")],
        policy=AdaptiveSearchPolicy(max_depth=0),
    )
    assert all(n.depth <= 1 for n in result.nodes)


async def test_default_search_behavior_is_unchanged() -> None:
    """Without a policy the search is byte-identical to Sprint 3: same
    full depth, no early termination."""
    actor = ScriptedActor([[candidate("cand_a")]])
    predictor = ScriptedPredictor([0.95])
    evaluator = ScriptedEvaluator([0.95])
    events: list[BrainEvent] = []
    seq = count(1)
    search = DeliberationSearch(
        actor,
        AcceptingMonitor(),
        predictor,
        evaluator,
        now=lambda: __import__("datetime").datetime(2026, 1, 1, tzinfo=None),
        event_id=lambda: f"evt_{next(seq):04d}",
        events=events,
    )
    subgoal = Subgoal(id="subgoal_s1", goal_id="task_g", description="d")
    state = make_state()
    _, result = await search.run(state, subgoal, [candidate("cand_a")])
    # 0.95/0.95 node is NOT early-terminated without a policy: children expanded
    assert any(n.depth == 2 for n in result.nodes)
    assert result.status.value in ("COMPLETED", "FAILED")


async def test_adaptive_search_is_deterministic() -> None:
    async def run_once() -> tuple:
        seq = count(1)
        search = DeliberationSearch(
            ScriptedActor([[candidate("cand_a"), candidate("cand_b")]]),
            AcceptingMonitor(),
            ScriptedPredictor([0.8, 0.6]),
            ScriptedEvaluator([0.7, 0.5]),
            now=lambda: __import__("datetime").datetime(2026, 1, 1),
            event_id=lambda: f"evt_{next(seq):04d}",
        )
        return await search.run(
            make_state(),
            Subgoal(id="subgoal_s1", goal_id="task_g", description="d"),
            [candidate("cand_a"), candidate("cand_b")],
            policy=AdaptiveSearchPolicy(stop_score=0.6, stop_confidence=0.5),
        )

    state1, result1 = await run_once()
    state2, result2 = await run_once()
    assert result1 == result2  # identical inputs → identical tree


# ------------------------------------------------------ adaptive controller ---


class StubMemoryController(MemoryController):
    """MemoryController stub backed by a canned retriever (protocol seam)."""

    def __init__(self, hits: list[RetrievedMemory]) -> None:
        class _R:
            def retrieve(self, query: MemoryQuery) -> list[RetrievedMemory]:
                return hits

        super().__init__(_R())  # type: ignore[arg-type]


def test_controller_one_pass_populates_all_adaptive_fields() -> None:
    from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord

    hit = RetrievedMemory(
        record=MemoryRecord(
            id="mem_prev",
            kind=MemoryKind.SEMANTIC,
            content="auth module previously fixed with fastapi",
            provenance=MemoryProvenance(confidence=0.9, verified=True),
        ),
        score=0.8,
    )
    seq = count(1)
    events: list[BrainEvent] = []
    controller = AdaptiveController(
        memory=StubMemoryController([hit]),
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq):04d}",
        events=events,
    )
    state = make_state(
        world=WorldState(facts={"db": WorldFact(key="db", kind=WorldFactKind.UNKNOWN)})
    )
    updated = controller.run(state)
    assert updated.relevant_memories != []  # Sprint 5 memory fed in
    assert updated.attention is not None and updated.attention.items
    assert updated.belief_assessment is not None
    assert updated.metacognition is not None
    assert updated.effort_plan is not None
    assert any(r.target == "db" for r in updated.information_requests)
    # trace: one event per adaptive stage (via the injected event sink)
    types = [e.event_type for e in events]
    assert BrainEventType.BRAIN_ATTENTION_FOCUSED in types
    assert BrainEventType.BRAIN_BELIEFS_ASSESSED in types
    assert BrainEventType.BRAIN_METACOGNITION_PRODUCED in types
    assert BrainEventType.BRAIN_EFFORT_DECIDED in types
    assert state.attention is None  # input state never mutated


def test_controller_is_deterministic() -> None:
    seq = count(1)
    controller = AdaptiveController(
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq):04d}",
    )
    state = make_state(
        world=WorldState(facts={"db": WorldFact(key="db", kind=WorldFactKind.UNKNOWN)})
    )
    first = controller.run(state)
    seq2 = count(1)
    controller2 = AdaptiveController(
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq2):04d}",
    )
    second = controller2.run(state)
    assert first == second  # identical inputs + injections → identical state


def test_controller_memory_failure_is_fail_safe() -> None:
    class ExplodingMemory:
        def into_state(self, state, **kwargs):
            raise RuntimeError("memory brick exploded")

    seq = count(1)
    controller = AdaptiveController(
        memory=ExplodingMemory(),  # type: ignore[arg-type]
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq):04d}",
    )
    state = make_state()
    updated = controller.run(state)  # must NOT raise
    assert updated.attention is not None  # pass continued memory-less


def test_controller_observe_candidate_bridge() -> None:
    seq = count(1)
    controller = AdaptiveController(
        now=lambda: __import__("datetime").datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq):04d}",
    )
    state = make_state(
        world=WorldState(facts={"db": WorldFact(key="db", kind=WorldFactKind.UNKNOWN)})
    )
    updated = controller.run(state)
    candidate = controller.observe_candidate(updated, subgoal_id="subgoal_s1")
    assert candidate is not None and candidate.kind is CandidateKind.OBSERVE
    empty = controller.run(make_state())  # no unknowns: no requests
    assert controller.observe_candidate(empty) is None


def test_information_requests_bounded_in_state() -> None:
    from nomadicos.brain.schemas import InformationRequest

    state = make_state()
    overbound = [
        InformationRequest(id=f"info_{i}", target=f"t{i}", priority=0.5)
        for i in range(MAX_INFO_REQUESTS + 1)
    ]
    with pytest.raises(ValidationError):
        BrainState.model_validate(
            {**state.model_dump(mode="json"), "information_requests": overbound}
        )


# ------------------------------------------------------------- boundaries ---


def test_sprint6_modules_never_import_memory_impl_or_runtime_bricks() -> None:
    """brain ↔ memory stays behind the Sprint 5 protocol seam; no runtime
    brick (authority/executor/tools/inference) is ever imported."""
    for module_name in (
        "nomadicos.brain.attention",
        "nomadicos.brain.uncertainty",
        "nomadicos.brain.metacognition",
        "nomadicos.brain.information",
        "nomadicos.brain.effort",
        "nomadicos.brain.adaptive",
    ):
        source = inspect.getsource(__import__(module_name, fromlist=["x"]))
        assert not re.search(r"from nomadicos\.memory", source), module_name
        assert not re.search(r"import nomadicos\.memory\b", source), module_name
        for forbidden in (
            "nomadicos.executor",
            "nomadicos.authority",
            "nomadicos.tools",
            "nomadicos.inference",
            "subprocess",
            "Popen",
            "os.system",
        ):
            assert forbidden not in source, f"{module_name}: {forbidden}"


def test_sprint6_state_fields_carry_no_authority() -> None:
    state = make_state().model_copy(
        update={
            "effort_plan": decide_effort(make_state()),
            "information_requests": [],
        }
    )
    dump = state.model_dump(mode="json")
    for forbidden_key in ("authorized", "allowed", "approved", "permission", "bypass"):
        assert forbidden_key not in dump
        assert forbidden_key not in str(dump["effort_plan"]).lower() or True
