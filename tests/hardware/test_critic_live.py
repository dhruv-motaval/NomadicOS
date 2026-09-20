"""HARDWARE §10.30/31: real Worker model + real independent CRITIC model.

qwen3:14b   = worker (implements, runs tests through the executor)
gemma3:4b   = critic role (separate inference call, separate model id);
              it is registered text-only-capable so it can never be
              selected as the worker for tool tasks (role separation).
Everything else is the production path: Action IR -> validation ->
authority -> executor -> verification. Outcomes are reported, not
demanded; the hard invariants (critic verdicts stored honestly, stored ACCEPT
impossible pre-verifier, SUCCESS only with verifier evidence) do hold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.coding_helpers import BUGGY_CALC, CALC_TEST
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry

pytestmark = pytest.mark.hardware

WORKER = "qwen3:14b"
CRITIC = "gemma3:4b"


def _registry() -> ModelRegistry:
    registry = ModelRegistry(EventLogger())
    registry.register(
        ModelRecord(
            model_id=WORKER,
            engine="ollama",
            roles=["coding", "worker"],
            capabilities=[
                CapabilityTag.TEXT,
                CapabilityTag.TOOL_USE,
                CapabilityTag.CODING,
                CapabilityTag.REASONING,
                CapabilityTag.TESTING,
            ],
            health=ModelHealth.HEALTHY,
            source="ollama",
            context_window=32768,
        )
    )
    registry.register(
        ModelRecord(
            model_id=CRITIC,
            engine="ollama",
            roles=["critic", "reasoning"],
            capabilities=[CapabilityTag.TEXT],
            health=ModelHealth.HEALTHY,
            source="ollama",
            context_window=8192,
        )
    )
    return registry


async def _require_models(app: NomadicApp) -> None:
    health = await app.engines["ollama"].health()
    if WORKER not in health.models or CRITIC not in health.models:
        pytest.skip("worker/critic models not served by local ollama")


async def test_live_worker_and_live_critic_end_to_end(tmp_path: Path) -> None:
    app = NomadicApp(
        registry=_registry(), workspace_root=tmp_path / "ws", state_dir=tmp_path / "state"
    )
    try:
        await _require_models(app)
        app.store.grant_full_pc_autonomy(source="hardware_owner")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "calculator.py").write_text(BUGGY_CALC, encoding="utf-8")
        (repo / "test_calculator.py").write_text(CALC_TEST, encoding="utf-8")
        goal = (
            "Two-phase demo you must execute IN ORDER: (1) run the tests with "
            'command "pytest" args ["-q"] and observe the failure; (2) repair '
            "multiply in calculator.py to return a * b (keep add intact), then "
            "run the same test command again until it passes."
        )
        preds = [{"type": "tests_pass", "command": "pytest -q"}]
        summary, report = await app.run_coding(
            goal,
            repo=repo,
            predicates=preds,
            ask_owner=lambda c: "DENY",
            critic_model=CRITIC,
            task_id="wc-live",
        )
        st = app.graph.get_state({"configurable": {"thread_id": "nomadic:wc-live"}}).values
        iters = st.get("critic_iterations") or []
        print(
            "LIVE WORKER/CRITIC:",
            summary.status.value,
            "verdict",
            summary.goal_verdict,
            "| iterations:",
            [(i["decision"], i.get("score")) for i in iters],
            "| exits:",
            report.test_exits if report else [],
            "| repairs:",
            report.repairs_used if report else None,
        )
        assert all(i["decision"] in {"IMPROVE", "REJECT", "NOT_EVALUATED"} for i in iters), (
            "a stored critic decision can never be a bare ACCEPT pre-verifier"
        )
        for i in iters:
            assert i["model_id"] == CRITIC or i["model_id"] is None
            assert i.get("implementation_revision")
        if summary.status is TaskStatus.SUCCESS:
            assert summary.goal_verdict == "PASS"
            assert 0 in (report.test_exits if report else [])
        assert (repo / "test_calculator.py").read_text(encoding="utf-8") == CALC_TEST
    finally:
        await app.aclose()


async def test_live_critic_actually_evaluates_real_evidence(tmp_path: Path) -> None:
    """Critique entry guaranteed without fabricating a critic failure: the
    owner predicate records test success only via pytest flags that do not
    exist, so no legitimate run satisfies it and verify_goal always fails -
    the REAL critic must face REAL evidence. Hard asserts: live critic ran,
    and no critic verdict manufactures SUCCESS over the failing verifier."""
    app = NomadicApp(
        registry=_registry(), workspace_root=tmp_path / "ws", state_dir=tmp_path / "state"
    )
    try:
        await _require_models(app)
        app.store.grant_full_pc_autonomy(source="hardware_owner")
        repo = tmp_path / "repo2"
        repo.mkdir()
        (repo / "calculator.py").write_text(BUGGY_CALC, encoding="utf-8")
        (repo / "test_calculator.py").write_text(CALC_TEST, encoding="utf-8")
        goal = (
            "Fix multiply in calculator.py so it returns a * b (keep add intact). "
            "Run the plain test command pytest to confirm progress: command "
            '"pytest" args ["-q"]. Never modify test files. Then finish.'
        )
        # Owner predicate: unsatisfiable by any honest run (pytest's captured
        # output can never EQUAL this sentinel while also passing) and it is
        # NOT a step-attached expectation, so failure surfaces at VERIFY_GOAL
        # - which, with the critic wired, must flow through CRITIQUE first.
        preds = [
            {
                "type": "stdout_equals",
                "text": "CRITIC-DEMO-SENTINEL-9f3a2b",
                "command": "pytest -q",
            }
        ]
        summary, report = await app.run_coding(
            goal,
            repo=repo,
            predicates=preds,
            ask_owner=lambda c: "ALLOW",  # let the worker re-run pytest freely
            critic_model=CRITIC,
            task_id="wclear",
        )
        st = app.graph.get_state({"configurable": {"thread_id": "nomadic:wclear"}}).values
        iters = st.get("critic_iterations") or []
        print(
            "LIVE CRITIC RUN:",
            summary.status.value,
            "iterations:",
            [(i["decision"], i.get("score"), i.get("model_id")) for i in iters],
            "| exits:",
            report.test_exits if report else [],
        )
        assert iters, "critique MUST execute - the goal predicate is unsatisfiable"
        evaluated = [i for i in iters if i.get("report_id")]
        assert evaluated, "real critic inference produced a stored report"
        assert all(i["model_id"] == CRITIC for i in evaluated)
        assert summary.status is not TaskStatus.SUCCESS
        assert summary.status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL}
        assert (repo / "test_calculator.py").read_text(encoding="utf-8") == CALC_TEST
    finally:
        await app.aclose()
