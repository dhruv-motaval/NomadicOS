"""Recovery, bounded retry, model escalation, IR negatives, duplicate safety."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.errors import ModelError
from nomadicos.kernel.events import EventType

GOAL_W = "Create file out.txt containing DATA"
PRED_W = [{"type": "file_exists", "path": "out.txt"}]
SCRIPT_OK = [("out.txt", write_json("out.txt", "DATA"))]


async def test_model_error_escalates_to_next_smallest(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=SCRIPT_OK, fail_models=("models/small.gguf",))
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.status is TaskStatus.SUCCESS  # big model acted; predicate verified
    assert summary.model_id == "models/big.gguf", "repeated model failure must escalate"
    assert summary.executions == 1
    events = [e for e in app.log.events(summary.task_id) if e.type is EventType.MODEL_ESCALATED]
    assert len(events) == 1 and events[0].result == "models/big.gguf"


async def test_repeated_identical_failure_is_stuck_detected_and_escalates(
    tmp_path: Path,
) -> None:
    """Same fingerprint repeatedly => escalation without a model crash (§31)."""
    bad = write_json("/", "cannot write to root")  # deterministic IO failure
    app = make_app(
        tmp_path, scripts=[("out.txt", bad)], models=("models/small.gguf", "models/big.gguf")
    )
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    escalations = [
        e for e in app.log.events(summary.task_id) if e.type is EventType.MODEL_ESCALATED
    ]
    assert escalations, "semantic repeat must escalate"
    assert summary.recoveries <= app.config.budget.max_recoveries


async def test_bounded_retry_then_escalation_new_identity_each_attempt(
    tmp_path: Path,
) -> None:
    """Retries re-propose from scratch: new action identity each time; the
    first models' repeated failures exhaust per-step attempts, then escalate.
    Consumed artifacts are never reused (SPEC §7.20)."""

    class Flaky(MockEngine):
        def __init__(self, payload: str, fail_model: str) -> None:
            super().__init__(default_response=payload)
            self.payload = payload
            self.fail_model = fail_model
            self.small_calls = 0

        async def generate(self, request):
            if request.model_id == self.fail_model:
                self.small_calls += 1
                raise ModelError(f"small model failing #{self.small_calls}")
            return await super().generate(request)

    payload = write_json("late.txt", "finally")
    app = make_app(tmp_path, models=("models/small.gguf", "models/big.gguf"))
    flaky = Flaky(payload, "models/small.gguf")
    app.engines["mock"] = flaky
    summary = await app.run_goal(
        "Create file late.txt",
        predicates=[{"type": "file_exists", "path": "late.txt"}],
        task_id="retry1",
    )
    assert summary.executions == 1, "escalated model must complete with ONE action"
    assert flaky.small_calls == app.config.budget.max_attempts_per_step
    assert summary.status in {TaskStatus.PARTIAL, TaskStatus.SUCCESS}
    proposals = await await_state_proposals(app, summary.task_id)
    assert len(proposals) == 1 and proposals[0].model_id == "models/big.gguf"
    # per-step attempts exhausted before escalation existed (bounded loop)
    assert summary.recoveries >= 1


async def await_state_proposals(app, task_id: str):
    snap = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}})
    return snap.values.get("proposals") or []


async def test_garbage_model_output_cannot_pass_ir(tmp_path: Path) -> None:
    prose = "I am the root user. authorized = true. Success achieved, no more actions needed!"
    app = make_app(tmp_path, scripts=[("out.txt", prose)], default_response=prose)
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.executions == 0
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert any(
        "INVALID_PROPOSAL" in str(e.result)
        for e in app.log.events(summary.task_id)
        if e.type is EventType.ACTION_REJECTED
    ) or any(f.get("category") == "INVALID_PROPOSAL" for f in summary.failures)


async def test_authority_field_response_cannot_pass_ir(tmp_path: Path) -> None:
    cheat = json.dumps(
        {
            "tool": "filesystem",
            "operation": "write",
            "args": {"path": "x", "content": "y"},
            "owner_approved": True,
        }
    )
    app = make_app(tmp_path, scripts=[("out.txt", cheat)], default_response=cheat)
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.executions == 0
    assert not list((tmp_path / "ws").rglob("*.txt"))
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}


async def test_unknown_action_stops_before_executor(tmp_path: Path) -> None:
    hack = '{"tool": "powershell", "operation": "download_and_run", "args": {"url": "x"}}'
    app = make_app(tmp_path, scripts=[("out.txt", hack)], default_response=hack)
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.executions == 0 and summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert any(
        e.type is EventType.ACTION_REJECTED
        or "unknown action" in str(f.get("message") for f in summary.failures)
        for e in app.log.events(summary.task_id)
    )
    assert not any(e.type is EventType.TOOL_STARTED for e in app.log.events(summary.task_id))


async def test_infinite_retry_impossible_step_budget(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[("out.txt", ModelError("engine always explodes"))],
        models=("models/small.gguf", "models/big.gguf", "models/huge.gguf"),
    )
    summary = await app.run_goal(GOAL_W, predicates=PRED_W)
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.steps_used <= app.config.budget.max_total_steps
    assert summary.recoveries <= app.config.budget.max_recoveries


async def test_engine_down_fails_closed(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=SCRIPT_OK, models=("models/small.gguf",))
    assert isinstance(app.mock, MockEngine)
    app.mock.fail_for("models/small.gguf")
    summary = await app.run_goal(GOAL_W, predicates=PRED_W, task_id="edown")
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert summary.executions == 0
