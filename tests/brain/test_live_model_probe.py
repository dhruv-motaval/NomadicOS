"""HARDWARE probe (Sprint 2): ONE optional live-model path proving the MAP
core runs against a real local model through the SAME typed contracts.

Isolated from deterministic unit tests (pytest -m hardware). Requires a
running Ollama with gemma3:4b (see tests/hardware/test_real_model_path.py).
Skips automatically when Ollama is unreachable.
"""

from __future__ import annotations

import pytest

from nomadicos.brain.actor import ModelActor
from nomadicos.brain.cognition import StructuredGenerator
from nomadicos.brain.decomposer import ModelDecomposer
from nomadicos.brain.monitor import ChainedMonitor, SemanticMonitor
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal
from nomadicos.inference import OllamaEngine
from nomadicos.inference.structured import EngineStructuredGenerator

pytestmark = pytest.mark.hardware

MODEL = "gemma3:4b"


async def test_live_model_produces_validated_structured_output() -> None:
    engine = OllamaEngine()
    try:
        health = await engine.health()
    except Exception:
        pytest.skip("Ollama unreachable; live MAP probe not available")
    if health.status.value != "HEALTHY":
        pytest.skip("Ollama unhealthy; live MAP probe not available")

    generator: StructuredGenerator = EngineStructuredGenerator(engine, MODEL)
    goal = Goal.from_spec(
        "Create out.txt in the current directory",
        predicates=[{"type": "file_exists", "path": "out.txt"}],
    )
    state = BrainState.for_goal(goal, mission_id="mission_live_probe")

    decomposition = await ModelDecomposer(generator).decompose(state)  # type: ignore[arg-type]
    assert decomposition.task_graph.goal_id == goal.id
    assert decomposition.task_graph.nodes, "live model must produce >= 1 task"

    subgoal = decomposition.task_graph.subgoals[0]
    proposal = await ModelActor(generator).propose(state, subgoal)  # type: ignore[arg-type]
    assert len(proposal.candidates) >= 1
    assert all(c.subgoal_id == subgoal.id for c in proposal.candidates)

    monitor = ChainedMonitor(semantic=SemanticMonitor(generator))  # type: ignore[arg-type]
    results = await monitor.evaluate(state, subgoal, proposal, 1)  # type: ignore[arg-type]
    assert len(results) == len(proposal.candidates)
    for result in results:
        assert result.decision.value in {"ACCEPTED", "REJECTED"}
        assert result.candidate_id in {c.id for c in proposal.candidates}
