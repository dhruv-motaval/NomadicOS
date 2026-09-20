"""Phase 9 HOTFIX regressions: model exhaustion → VERIFY_GOAL when evidence
exists (never self-declared success; verifier still the only SUCCESS source)."""

from __future__ import annotations

import inspect
from pathlib import Path

from helpers import make_app

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.kernel.errors import Failure
from nomadicos.kernel.events import EventType

PREDS = [
    {"type": "exit_code_equals", "code": 1, "command": "pytest -q"},
    {"type": "tests_pass", "command": "pytest -q"},
]


def _ev(task: str, code: int | None, status: ExecutionStatus) -> ExecutionResult:
    return ExecutionResult(
        task_id=task,
        step_id="step_x",
        action_id="a",
        action_fingerprint=f"x{code}",
        model_id="models/small.gguf",
        tool="terminal",
        operation="execute",
        status=status,
        exit_code=code,
        stdout="",
        stderr="",
        evidence={"command": "pytest", "args": ["-q"]},
    )


def _graph_input(task: str, executions: list[ExecutionResult]) -> dict:
    return {
        "goal_text": "Demonstrate exhaustion routing (run pytest -q)",
        "goal_predicates": PREDS,
        "task_id": task,
        "executions": executions,
    }


def cfg(task: str) -> dict:
    return {"configurable": {"thread_id": f"nomadic:{task}"}}


async def test_exhaustion_with_sufficient_evidence_reaches_verify_goal_and_success(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, models=())  # no candidates: SELECT_MODEL exhaustion
    evs = [_ev("task_ok", 1, ExecutionStatus.FAILED), _ev("task_ok", 0, ExecutionStatus.SUCCEEDED)]
    final = await app.graph.ainvoke(_graph_input("task_ok", evs), cfg("task_ok"))
    # no terminal FAILED at select: the verifier ran last...
    assert final["model_exhausted"] is True
    assert final["task_status"] is TaskStatus.SUCCESS
    assert final["verifications"][-1].verdict.value == "PASS"
    goal_events = [e for e in app.log.events("task_ok") if e.type is EventType.GOAL_VERIFIED]
    assert goal_events and goal_events[-1].result == "PASS"


async def test_exhaustion_with_insufficient_evidence_verifier_says_not_pass(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, models=())  # no candidates: SELECT_MODEL exhaustion
    evs = [_ev("task_bad", 1, ExecutionStatus.FAILED)]  # no exit-0 run ever
    final = await app.graph.ainvoke(_graph_input("task_bad", evs), cfg("task_bad"))
    goal_events = [e for e in app.log.events("task_bad") if e.type is EventType.GOAL_VERIFIED]
    assert goal_events, "verifier MUST get evaluated evidence even when it loses"
    assert goal_events[-1].result == "NOT_PASS"
    assert final["task_status"] in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert final["task_status"] is not TaskStatus.SUCCESS


async def test_exhaustion_without_executions_still_fails(tmp_path: Path) -> None:
    app = make_app(tmp_path, models=())  # no candidates: SELECT_MODEL exhaustion
    final = await app.graph.ainvoke(_graph_input("task_empty", []), cfg("task_empty"))
    assert final["task_status"] is TaskStatus.FAILED
    assert not [e for e in app.log.events("task_empty") if e.type is EventType.GOAL_VERIFIED]


async def test_exhaustion_records_typed_failure_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, models=())  # no candidates: SELECT_MODEL exhaustion
    evs = [
        _ev("task_keep", 1, ExecutionStatus.FAILED),
        _ev("task_keep", 0, ExecutionStatus.SUCCEEDED),
    ]
    final = await app.graph.ainvoke(_graph_input("task_keep", evs), cfg("task_keep"))
    cats = [f.category for f in final["failures"]]
    assert Failure.RESOURCE_UNAVAILABLE in cats  # exhaustion recorded, typed
    assert len(final["executions"]) == 2  # evidence not destroyed by routing death
    assert final["model_exhausted"] is True


def test_success_write_site_count_remains_exactly_one() -> None:
    import nomadicos.orchestration.graph as g

    src = inspect.getsource(g)
    assert src.count('"task_status": TaskStatus.SUCCESS') == 1
    # route_select gained the verify_goal edge but holds no authority itself
    assert '"verify_goal"' in src[src.index("def route_select") :]
