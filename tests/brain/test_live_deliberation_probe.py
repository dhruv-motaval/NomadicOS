"""HARDWARE live-model Deliberation probe for NomadicOS.

Exercises ModelPredictor, ModelEvaluator, and bounded DeliberationSearch
against a live llama.cpp server serving models/Spark-X2.5-4B-uncensored-Q4_K_M.gguf.

Marked hardware: isolated from offline unit tests (pytest -m hardware).
Skips honestly only if llama-server is genuine unavailable.
Fails clearly on any schema violation.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from nomadicos.brain.actor import MAX_CANDIDATES_PER_PROPOSAL, ActorProposal, ModelActor
from nomadicos.brain.cognition import StructuredGenerator
from nomadicos.brain.deliberation import (
    DeliberationSearch,
    ModelEvaluator,
    ModelPredictor,
    select_action,
)
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.monitor import MonitorDecision, MonitorStage
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.schemas import (
    MAX_SEARCH_NODES,
    SEARCH_BRANCHING,
    CandidateKind,
    Capability,
    Decision,
    Evaluation,
    EvaluationFactors,
    MonitorResult,
    Prediction,
    SearchResult,
    SearchStatus,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.model import ModelHealth
from nomadicos.inference.llama_cpp import LlamaCppEngine
from nomadicos.inference.structured import EngineStructuredGenerator

pytestmark = pytest.mark.hardware

MODEL_ID = "models/Spark-X2.5-4B-uncensored-Q4_K_M.gguf"
BASE_URL = "http://127.0.0.1:8080"


async def _disable_thinking_hook(request: httpx.Request) -> None:
    """Disable thinking tokens for reasoning models (e.g. Spark-X2.5).

    When enable_thinking is true, reasoning models exhaust tokens in chain-of-thought
    before emitting final content. Disabling thinking instructs the chat template
    to produce direct, concise structured JSON.
    """
    if b"/v1/chat/completions" in request.url.raw_path and request.content:
        body = json.loads(request.content.decode("utf-8"))
        body.setdefault("chat_template_kwargs", {})["enable_thinking"] = False
        new_content = json.dumps(body).encode("utf-8")
        request.headers["content-length"] = str(len(new_content))
        request.stream = httpx.ByteStream(new_content)


class TerminalScriptedActor:
    """Actor fake: returns no further children, making depth-1 branches terminal."""

    async def propose(
        self,
        state: BrainState,
        subgoal: Subgoal,
        feedback: Any = (),
        iteration: int = 1,
    ) -> ActorProposal:
        return ActorProposal(subgoal_id=subgoal.id, iteration=iteration, candidates=[])


class AcceptingMonitor:
    """Monitor fake: accepts proposed candidates structurally."""

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


@pytest.mark.asyncio
async def test_live_model_deliberation_flow() -> None:
    """Prove ModelActor, ModelPredictor, ModelEvaluator, DeliberationSearch
    and select_action against live llama-server with ACTOR-GENERATED
    candidates (not hand-crafted ones)."""
    client = httpx.AsyncClient(
        base_url=BASE_URL,
        event_hooks={"request": [_disable_thinking_hook]},
        timeout=120.0,
    )
    engine = LlamaCppEngine(client=client)

    try:
        # Readiness check: the ONLY legitimate skip point (server unavailable).
        # Once the server passes this check, all later failures are FAILs.
        try:
            health = await engine.health()
        except Exception as exc:
            pytest.skip(f"llama-server unreachable at {BASE_URL}: {exc}")

        if health.status is not ModelHealth.HEALTHY:
            pytest.fail(f"llama-server reachable but not healthy at {BASE_URL}: {health.detail!r}")
        if MODEL_ID not in health.models:
            pytest.fail(
                f"Configured model {MODEL_ID!r} not exposed by llama-server "
                f"(available: {health.models})"
            )

        generator: StructuredGenerator = EngineStructuredGenerator(
            engine, MODEL_ID, max_tokens=1024, temperature=0.1
        )

        # Build goal, task graph, active subgoal, and the registered
        # capability view the actor names its proposals against.
        goal = Goal(
            id="task_live_g1",
            objective="Create report file report.txt",
            predicates=[parse_predicate({"type": "file_exists", "path": "report.txt"})],
        )
        subgoal = Subgoal(id="sub_s1", goal_id=goal.id, description="Generate report file")
        task = TaskNode(id="t1", description=subgoal.description, subgoal_id=subgoal.id)
        task_graph = TaskGraph.build(goal_id=goal.id, nodes=[task], subgoals=[subgoal])
        capabilities = [
            Capability(name=f"filesystem.{op}")
            for op in ("write", "read", "exists", "list", "mkdir")
        ]
        world = WorldState(
            version=1,
            facts={
                "target_file": WorldFact(
                    key="target_file", value="report.txt", kind=WorldFactKind.FACT
                ),
                "target_content": WorldFact(
                    key="target_content", value="status: ok", kind=WorldFactKind.FACT
                ),
            },
        )

        state = BrainState.for_goal(goal, task_graph=task_graph, mission_id="mission_live_probe")
        state = state.model_copy(
            update={
                "active_subgoal": subgoal,
                "capabilities": capabilities,
                "world": world,
            }
        )

        # ----------------------------------------------------------------- #
        # 1. ModelActor with live model inference: actor-GENERATED candidates
        # ----------------------------------------------------------------- #
        actor = ModelActor(generator)
        t0 = time.monotonic()
        proposal: ActorProposal | None = None
        actor_error: str = ""
        for _ in range(3):
            try:
                proposal = await actor.propose(state, subgoal, iteration=1)
                break
            except ModelOutputRejected as exc:
                actor_error = str(exc)
        latency_actor = time.monotonic() - t0
        assert proposal is not None, (
            f"ModelActor failed to produce a valid proposal in 3 bounded attempts "
            f"(last rejection: {actor_error!r}). Server was healthy, so this is a "
            f"model-output failure, not a skip."
        )
        candidates = list(proposal.candidates)
        assert 1 <= len(candidates) <= MAX_CANDIDATES_PER_PROPOSAL
        for c in candidates:
            assert c.subgoal_id == subgoal.id
            assert c.capability in {cap.name for cap in capabilities}, (
                f"candidate {c.id} capability {c.capability!r} is not a registered capability"
            )
            if c.kind is CandidateKind.EXECUTE:
                assert c.arguments.get("path"), (
                    f"EXECUTE candidate {c.id} has no 'path' argument: {c.arguments!r}"
                )
                if c.capability == "filesystem.write":
                    assert c.arguments.get("content"), (
                        f"filesystem.write candidate {c.id} has no 'content' argument: "
                        f"{c.arguments!r}"
                    )
        print(
            f"\n[LIVE PROBE] Actor: latency={latency_actor:.2f}s, "
            f"candidates={[(c.id, c.capability) for c in candidates]}"
        )

        # ----------------------------------------------------------------- #
        # 2. AcceptingMonitor — structural gate on actor-generated candidates
        # ----------------------------------------------------------------- #
        monitor = AcceptingMonitor()
        state = state.model_copy(update={"action_candidates": candidates})
        monitor_results = await monitor.evaluate(state, subgoal, proposal, iteration=1)
        accepted_ids = {
            r.candidate_id for r in monitor_results if r.decision is MonitorDecision.ACCEPTED
        }
        assert accepted_ids, "Monitor accepted no candidates"
        accepted = [c for c in candidates if c.id in accepted_ids]
        target = accepted[0]

        # ----------------------------------------------------------------- #
        # 3. ModelPredictor with live model inference (actual candidate)
        # ----------------------------------------------------------------- #
        predictor = ModelPredictor(generator)
        t0 = time.monotonic()
        pred: Prediction = await predictor.predict(state, target, subgoal)
        latency_pred = time.monotonic() - t0

        # Validate prediction against Pydantic schema
        assert isinstance(pred, Prediction)
        assert pred.candidate_id == target.id
        assert isinstance(pred.confidence, float)
        assert 0.0 <= pred.confidence <= 1.0
        assert isinstance(pred.predicted_world, WorldState)
        assert len(pred.predicted_world.facts) >= 1
        for key, fact in pred.predicted_world.facts.items():
            assert fact.kind is WorldFactKind.PREDICTION, (
                f"Predicted fact {key!r} kind must be PREDICTION, got {fact.kind}"
            )
        assert isinstance(pred.assumptions, list)
        assert isinstance(pred.uncertainties, list)
        print(f"[LIVE PROBE] Predictor: latency={latency_pred:.2f}s, confidence={pred.confidence}")

        # ----------------------------------------------------------------- #
        # 4. ModelEvaluator with live model inference (actual prediction)
        # ----------------------------------------------------------------- #
        evaluator = ModelEvaluator(generator)
        t0 = time.monotonic()
        ev: Evaluation = await evaluator.evaluate(state, target, pred, subgoal)
        latency_eval = time.monotonic() - t0

        # Validate evaluation against Pydantic schema
        assert isinstance(ev, Evaluation)
        assert ev.candidate_id == target.id
        assert ev.prediction_id == pred.id
        assert isinstance(ev.score, float)
        assert 0.0 <= ev.score <= 1.0
        assert isinstance(ev.factors, EvaluationFactors)
        for factor_name in (
            "goal_progress",
            "feasibility",
            "risk",
            "uncertainty",
            "resource_cost",
            "reversibility",
        ):
            val = getattr(ev.factors, factor_name)
            assert 0.0 <= val <= 1.0, f"Evaluation factor {factor_name} out of bounds: {val}"
        assert ev.rationale.strip(), "ModelEvaluator must produce an explanatory rationale"
        print(f"[LIVE PROBE] Evaluator: latency={latency_eval:.2f}s, score={ev.score}")

        # ----------------------------------------------------------------- #
        # 5. Bounded DeliberationSearch with live Predictor & Evaluator
        # ----------------------------------------------------------------- #
        child_actor = TerminalScriptedActor()
        search = DeliberationSearch(child_actor, monitor, predictor, evaluator)

        t0 = time.monotonic()
        next_state, result = await search.run(state, subgoal, accepted)
        latency_search = time.monotonic() - t0

        assert isinstance(result, SearchResult)
        assert result.status is SearchStatus.COMPLETED, (
            f"DeliberationSearch failed: {result.failure_reason!r}. "
            f"Nodes: {[(n.node_id, n.status.value) for n in result.nodes]}. "
            "Server was healthy — qualification failure, not a skip."
        )
        assert len(result.nodes) <= MAX_SEARCH_NODES
        assert result.root_candidate_ids == [c.id for c in accepted[:SEARCH_BRANCHING]]
        print(f"[LIVE PROBE] Search: latency={latency_search:.2f}s, nodes={len(result.nodes)}")

        # ----------------------------------------------------------------- #
        # 6. Action Selection: eligible monitor-accepted candidate check
        # ----------------------------------------------------------------- #
        decision: Decision = select_action(result)
        assert isinstance(decision, Decision)
        assert decision.selected is True
        # Candidate must come from the eligible monitor-accepted set
        assert decision.candidate_id in accepted_ids, (
            f"Selected {decision.candidate_id!r} not in monitor-accepted set {sorted(accepted_ids)}"
        )
        selected_node = decision.factors.get("node_id")
        assert selected_node in {f"root.{i}" for i in range(len(accepted[:SEARCH_BRANCHING]))}
        prop_val = decision.factors.get("propagated_value")
        assert prop_val is not None
        print(f"[LIVE PROBE] Selected Candidate: {decision.candidate_id} (value={prop_val})")

        # ----------------------------------------------------------------- #
        # 7. Planning-only boundary: zero execution or authority tokens
        # ----------------------------------------------------------------- #
        assert not hasattr(decision, "authorized")
        assert not hasattr(decision, "executed")
        assert next_state.world.facts == state.world.facts

    finally:
        await engine.aclose()
