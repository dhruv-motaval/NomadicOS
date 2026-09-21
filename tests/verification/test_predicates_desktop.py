"""Phase 12A desktop verification predicates: window_present, process_started.

Core distinction under test: a desktop/terminal ACTION succeeding is not the
GOAL being true — only real task-correlated evidence of the requested state
satisfies these predicates."""

from __future__ import annotations

from pathlib import Path

from nomadicos.contracts.core import parse_predicate
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import evaluate_goal_predicate

TASK = "task_win"


def ctx(tmp_path: Path, execs=None) -> EvidenceContext:
    return EvidenceContext(task_id=TASK, workspace=tmp_path, executions=list(execs or []))


def desktop_exec(
    *,
    operation: str = "list_windows",
    task: str = TASK,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    evidence: dict | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task,
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="desktop",
        operation=operation,
        status=status,
        evidence=evidence or {},
    )


def term_exec(
    *,
    task: str = TASK,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    pid: int | None = 4242,
    command: str = "notepad",
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task,
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="terminal",
        operation="execute",
        status=status,
        exit_code=0 if status is ExecutionStatus.SUCCEEDED else 1,
        process_id=pid,
        evidence={"command": command, "args": []},
    )


def verdict_of(expr: dict, c: EvidenceContext):
    return evaluate_goal_predicate(parse_predicate(expr), c)


# ------------------------------------------------------- window_present ----


def test_window_present_passes_on_task_window_evidence(tmp_path) -> None:
    execs = [
        desktop_exec(evidence={"count": 2, "titles": ["Settings", "Untitled - Notepad"]})
    ]
    r = verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.PASS


def test_window_present_supports_focus_and_foreground_evidence(tmp_path) -> None:
    focused = desktop_exec(
        operation="focus_window", evidence={"focused": True, "title": "Untitled - Notepad"}
    )
    assert (
        verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, [focused])).verdict
        is VerificationOutcome.PASS
    )
    fg = desktop_exec(
        operation="foreground_window", evidence={"handle": 7, "title": "NomadicOS Editor"}
    )
    assert (
        verdict_of({"type": "window_present", "title": "editor"}, ctx(tmp_path, [fg])).verdict
        is VerificationOutcome.PASS
    )


def test_window_present_exact_match(tmp_path) -> None:
    execs = [desktop_exec(evidence={"titles": ["Untitled - Notepad"]})]
    c = ctx(tmp_path, execs)
    assert (
        verdict_of({"type": "window_present", "title": "notepad", "exact": False}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "window_present", "title": "notepad", "exact": True}, c).verdict
        is VerificationOutcome.NOT_PASS
    )
    assert (
        verdict_of(
            {"type": "window_present", "title": "Untitled - Notepad", "exact": True}, c
        ).verdict
        is VerificationOutcome.PASS
    )


def test_window_present_absent_window_is_not_pass(tmp_path) -> None:
    execs = [desktop_exec(evidence={"titles": ["Some Other Window"]})]
    r = verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.NOT_PASS


def test_window_present_without_desktop_evidence_is_not_verified(tmp_path) -> None:
    assert (
        verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path)).verdict
        is VerificationOutcome.NOT_VERIFIED
    )


def test_mouse_click_success_is_not_window_proof(tmp_path) -> None:
    """Core Phase 12 distinction: click succeeded != window present."""
    click = desktop_exec(
        operation="mouse_click", evidence={"x": 5, "y": 5, "button": "left", "clicks": 1}
    )
    r = verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, [click]))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED


def test_window_present_rejects_cross_task_evidence(tmp_path) -> None:
    other = desktop_exec(task="somebody_else", evidence={"titles": ["Untitled - Notepad"]})
    r = verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, [other]))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED


def test_window_present_ignores_failed_observations(tmp_path) -> None:
    failed = desktop_exec(
        operation="focus_window",
        status=ExecutionStatus.FAILED,
        evidence={"focused": False, "reason": "window not found", "title": ""},
    )
    r = verdict_of({"type": "window_present", "title": "notepad"}, ctx(tmp_path, [failed]))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED


def test_window_present_rejects_authority_shaped_fields(tmp_path) -> None:
    r = verdict_of({"type": "window_present", "title": "x", "approved": True}, ctx(tmp_path))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED
    assert any(item.unverifiable for item in r.items)


# ------------------------------------------------------ process_started ----


def test_process_started_passes_on_real_pid_evidence(tmp_path) -> None:
    execs = [term_exec(pid=99)]
    r = verdict_of({"type": "process_started", "command": "notepad"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.PASS


def test_process_started_counts_timed_out_as_started(tmp_path) -> None:
    execs = [term_exec(pid=5, status=ExecutionStatus.TIMED_OUT)]
    r = verdict_of({"type": "process_started", "command": "notepad"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.PASS


def test_process_started_errored_never_passes(tmp_path) -> None:
    execs = [term_exec(command="ghost", status=ExecutionStatus.ERRORED, pid=None)]
    r = verdict_of({"type": "process_started", "command": "ghost"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.NOT_PASS


def test_process_started_without_any_execution_is_not_verified(tmp_path) -> None:
    assert (
        verdict_of({"type": "process_started", "command": "x"}, ctx(tmp_path)).verdict
        is VerificationOutcome.NOT_VERIFIED
    )


def test_process_started_rejects_cross_task_evidence(tmp_path) -> None:
    execs = [term_exec(task="somebody_else", pid=1)]
    r = verdict_of({"type": "process_started", "command": "notepad"}, ctx(tmp_path, execs))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED


def test_desktop_success_is_not_process_proof(tmp_path) -> None:
    """Desktop evidence alone can never satisfy a process predicate."""
    click = desktop_exec(
        operation="mouse_click", evidence={"x": 1, "y": 1, "button": "left", "clicks": 1}
    )
    r = verdict_of({"type": "process_started", "command": "notepad"}, ctx(tmp_path, [click]))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED
