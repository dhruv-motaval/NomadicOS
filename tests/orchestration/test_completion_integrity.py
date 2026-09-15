"""Completion integrity (SPEC §28, §7.16, §30 no Phase-8 cheating)."""

from __future__ import annotations

from pathlib import Path

from helpers import make_app, write_json

from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationResult,
)
from nomadicos.kernel.events import EventType


def _result(
    level: VerificationLevel, passed_observed: bool, claim: str, task_id: str = "t"
) -> VerificationResult:
    return VerificationResult(
        level=level,
        task_id=task_id,
        evidence=[EvidenceItem(claim=claim, observed=passed_observed)],
    )


class PassingGoalDouble:
    """TEST DOUBLE ONLY: proves the graph HONORS a real verifier's evidence.
    Product code ships no verifier (that is Phase 8)."""

    def verify(self, goal: Goal) -> VerificationResult:
        return _result(VerificationLevel.GOAL, True, "double says predicates observed")

    def verify_task(self, goal, executions) -> VerificationResult:
        assert executions, "must not pass on empty evidence"
        return self.verify(goal)


class FailingGoalDouble:
    def verify(self, goal: Goal) -> VerificationResult:
        return _result(VerificationLevel.GOAL, False, "double: predicate not satisfied")

    def verify_task(self, goal, executions) -> VerificationResult:
        return self.verify(goal)


async def test_model_completion_claim_alone_never_yields_success(tmp_path: Path) -> None:
    app = make_app(tmp_path, models=("models/small.gguf",))  # default: {"finished": true}
    summary = await app.run_goal("Make the greatest file ever", task_id="claim1")
    assert summary.status is TaskStatus.PARTIAL, "finished=true is a CLAIM, not SUCCESS (§28)"
    assert summary.executions == 0
    statuses = {e.result for e in app.log.events(summary.task_id)}
    assert "SUCCESS" not in statuses


async def test_successful_actions_alone_never_yield_success(tmp_path: Path) -> None:
    """No completion predicates declared => truth undecidable => NOT SUCCESS,
    no matter how many actions executed (SPEC §8.9)."""
    app = make_app(tmp_path, scripts=[("x.txt", write_json("x.txt", "1"))])
    summary = await app.run_goal("Create file x.txt")  # predicate deliberately absent
    assert summary.executions >= 1  # a real action ran
    assert summary.status is TaskStatus.PARTIAL  # ... proves nothing by itself
    assert summary.goal_verdict in {None, "NOT_VERIFIED"}


async def test_no_failures_never_implies_success(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=[("y.txt", write_json("y.txt", "2"))])
    summary = await app.run_goal("Create file y.txt")  # no predicates to satisfy
    assert not summary.failures
    assert summary.status is not TaskStatus.SUCCESS


async def test_missing_verifier_path_is_labeled_partial_and_audited(tmp_path: Path) -> None:
    from nomadicos.orchestration.boundaries import NoGoalVerifier

    app = make_app(tmp_path, scripts=[("z.txt", write_json("z.txt", "3"))])
    # DI: simulate a deployment WITHOUT the Phase 8 verifier installed
    app.runtime.goal_verifier = NoGoalVerifier()
    summary = await app.run_goal(
        "Create file z.txt", predicates=[{"type": "file_exists", "path": "z.txt"}]
    )
    assert (
        "not independently" in summary.outcome_note.lower()
        or "phase 8" in summary.outcome_note.lower()
    )
    assert any(
        "NOT_PASSED" in str(e.result)
        for e in app.log.events(summary.task_id)
        if e.type is EventType.GOAL_VERIFIED
    )


async def test_success_requires_real_verifier_evidence_double(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[
            ("done.txt", write_json("done.txt", "ok")),
            ("done.txt again", '{"finished": true}'),
        ],
    )
    app.runtime.goal_verifier = PassingGoalDouble()
    # two-step goal; after the first write, second script claims finished
    summary = await app.run_goal(
        "Create file done.txt containing ok",
        predicates=[{"type": "file_exists", "path": "done.txt"}],
    )
    assert summary.executions == 1
    # single-predicate plan finishes -> claim loop: default '{"finished": true}'
    # response after the step completes -> verify_goal -> verifier PASSes
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert any(
        e.type is EventType.TASK_COMPLETED and e.result == "SUCCESS"
        for e in app.log.events(summary.task_id)
    )
    # and SUCCESS reached ONLY through the double's evidence, never before it


async def test_verifier_not_pass_routes_to_bounded_recovery(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=[("no.txt", write_json("no.txt", "x"))])
    app.runtime.goal_verifier = FailingGoalDouble()
    summary = await app.run_goal(
        "Create no.txt", predicates=[{"type": "file_exists", "path": "no.txt"}]
    )
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.recoveries <= app.config.budget.max_recoveries


def test_execution_result_contract_has_no_success_semantics() -> None:
    """Step results cannot claim task completion by construction."""
    fields = set(ExecutionResult.model_fields)
    assert "task_success" not in fields and "goal_complete" not in fields
    ok = ExecutionResult(
        task_id="t",
        step_id="s",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="terminal",
        operation="execute",
        status=ExecutionStatus.SUCCEEDED,
        exit_code=0,
    )
    assert ok.succeeded and not hasattr(ok, "goal_verified")
