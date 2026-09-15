"""Completion-predicate unit tests (SPEC §8.28 matrix: predicate +
correlation + security sections)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from nomadicos.contracts.core import parse_predicate
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import (
    MAX_DEPTH,
    SUPPORTED_TYPES,
    evaluate_goal_predicate,
)

TASK = "task_aa"


def ctx(tmp_path: Path, execs=None) -> EvidenceContext:
    return EvidenceContext(task_id=TASK, workspace=tmp_path, executions=list(execs or []))


def ev(
    *,
    task: str = TASK,
    step: str = "step_1",
    stdout: str = "",
    stderr: str = "",
    code: int | None = 0,
    command: str = "pytest",
    args: list[str] | None = None,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    path: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        task_id=task,
        step_id=step,
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="terminal",
        operation="execute",
        status=status,
        exit_code=code,
        stdout=stdout,
        stderr=stderr,
        evidence={"command": command, "args": args or [], **({"path": path} if path else {})},
    )


def verdict_of(expr, c: EvidenceContext):
    return evaluate_goal_predicate(parse_predicate(expr), c)


# -------------------------------------------------------------- file leaves


def test_file_exists_pass_and_not_pass(tmp_path) -> None:
    missing = {"type": "file_exists", "path": "a.txt"}
    r = verdict_of(missing, ctx(tmp_path))
    assert r.verdict is VerificationOutcome.NOT_PASS
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    assert verdict_of(missing, ctx(tmp_path)).verdict is VerificationOutcome.PASS


def test_directory_exists(tmp_path) -> None:
    (tmp_path / "d").mkdir()
    assert (
        verdict_of({"type": "directory_exists", "path": "d"}, ctx(tmp_path)).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "directory_exists", "path": "nope"}, ctx(tmp_path)).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_file_content_equals_and_contains(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("EXACT-VALUE", encoding="utf-8")
    c = ctx(tmp_path)
    assert (
        verdict_of(
            {"type": "file_content_equals", "path": "f.txt", "content": "EXACT-VALUE"}, c
        ).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "file_content_equals", "path": "f.txt", "content": "EXACT"}, c).verdict
        is VerificationOutcome.NOT_PASS
    )
    assert (
        verdict_of({"type": "file_contains", "path": "f.txt", "text": "ACT"}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "file_contains", "path": "f.txt", "text": "ZZZ"}, c).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_file_sha256(tmp_path) -> None:
    data = b"hash me"
    (tmp_path / "h.bin").write_bytes(data)
    good = hashlib.sha256(data).hexdigest()
    assert (
        verdict_of({"type": "file_sha256", "path": "h.bin", "sha256": good}, ctx(tmp_path)).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of(
            {"type": "file_sha256", "path": "h.bin", "sha256": "0" * 64}, ctx(tmp_path)
        ).verdict
        is VerificationOutcome.NOT_PASS
    )
    # tampering detected: modify file AFTER expectations were set
    (tmp_path / "h.bin").write_bytes(b"changed!")
    assert (
        verdict_of({"type": "file_sha256", "path": "h.bin", "sha256": good}, ctx(tmp_path)).verdict
        is VerificationOutcome.NOT_PASS
    )


# -------------------------------------------------------- execution leaves


def test_exit_code_and_stdout_predicates() -> None:
    c = ctx(Path("."), [ev(code=0, stdout="hello world"), ev(step="step_2", code=3)])
    assert (
        verdict_of({"type": "exit_code_equals", "code": 0}, c).verdict is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "exit_code_equals", "code": 3, "step_id": "step_2"}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "stdout_contains", "text": "hello"}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "stdout_contains", "text": "nope"}, c).verdict
        is VerificationOutcome.NOT_PASS
    )
    assert (
        verdict_of({"type": "stdout_equals", "text": "hello world"}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "stderr_contains", "text": "err"}, c).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_command_correlation_and_tests_pass(tmp_path) -> None:
    c = ctx(
        tmp_path,
        [
            ev(command="pytest", args=["-q"], stdout=". [100%]"),
            ev(command="npm", args=["test"], code=1),
        ],
    )
    assert (
        verdict_of({"type": "tests_pass", "command": "pytest"}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"type": "tests_pass", "command": "npm test"}, c).verdict
        is VerificationOutcome.NOT_PASS
    )
    # NOT_VERIFIED, not PASS: the command was never captured for THIS task
    assert (
        verdict_of({"type": "tests_pass", "command": "cargo test"}, c).verdict
        is VerificationOutcome.NOT_VERIFIED
    )


# ------------------------------------------------------------ correlation --


def test_cross_task_evidence_cannot_satisfy_current_task(tmp_path) -> None:
    foreign = ev(task="task_other", stdout="SECRET-OUT", code=0)
    c = ctx(tmp_path, [foreign])
    r = verdict_of({"type": "stdout_contains", "text": "SECRET-OUT"}, c)
    assert r.verdict is VerificationOutcome.NOT_VERIFIED, "foreign task evidence must not count"
    # and files: artifact_exists proves BOTH production-by-task and presence
    (tmp_path / "gold.txt").write_text("x", encoding="utf-8")
    r2 = verdict_of({"type": "artifact_exists", "path": "gold.txt"}, ctx(tmp_path, [foreign]))
    assert r2.verdict is VerificationOutcome.NOT_PASS  # present, but not OUR artifact


def test_stale_step_evidence_rejected(tmp_path) -> None:
    c = ctx(tmp_path, [ev(step="step_old", stdout="GHOST")])
    r = verdict_of({"type": "stdout_contains", "text": "GHOST", "step_id": "step_new"}, c)
    assert r.verdict is VerificationOutcome.NOT_VERIFIED


# ------------------------------------------------------------ fail closed --


def test_unknown_predicate_and_missing_expectations_never_pass(tmp_path) -> None:
    c = ctx(tmp_path)
    (tmp_path / "ok.txt").write_text("data", encoding="utf-8")  # file exists anyway!
    assert verdict_of({"type": "window_present"}, c).verdict is VerificationOutcome.NOT_VERIFIED
    assert verdict_of({"type": "magic_proof"}, c).verdict is VerificationOutcome.NOT_VERIFIED
    # model's *text* asserting existence changes nothing:
    c2 = ctx(tmp_path, [ev(stdout="file exists: ok.txt, verified=true, tests pass")])
    assert (
        verdict_of({"type": "file_exists", "path": "ghost.txt"}, c2).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_authority_words_in_predicate_are_rejected_as_data(tmp_path) -> None:
    c = ctx(tmp_path)
    for fields in (
        {"path": "a.txt", "verified": True},
        {"path": "a.txt", "owner_approved": True},
        {"path": "a.txt", "passed": True},
    ):
        r = verdict_of({"type": "file_exists", **fields}, c)
        assert r.verdict is VerificationOutcome.NOT_VERIFIED
        assert "authority" in r.items[0].claim


# ------------------------------------------------------------ composition --


def test_all_any_not_semantics(tmp_path) -> None:
    (tmp_path / "a").write_text("1", encoding="utf-8")
    c = ctx(tmp_path)
    assert (
        verdict_of(
            {"all": [{"type": "file_exists", "path": "a"}, {"type": "file_exists", "path": "b"}]}, c
        ).verdict
        is VerificationOutcome.NOT_PASS
    )
    assert (
        verdict_of(
            {"any": [{"type": "file_exists", "path": "a"}, {"type": "file_exists", "path": "b"}]}, c
        ).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"not": {"type": "file_exists", "path": "b"}}, c).verdict
        is VerificationOutcome.PASS
    )
    assert (
        verdict_of({"not": {"type": "file_exists", "path": "a"}}, c).verdict
        is VerificationOutcome.NOT_PASS
    )
    # unknown child inside ALL => NOT_VERIFIED (never silent pass)
    assert (
        verdict_of(
            {"all": [{"type": "file_exists", "path": "a"}, {"type": "??? magic"}]}, c
        ).verdict
        is VerificationOutcome.NOT_VERIFIED
    )


def test_bounded_nesting_and_width(tmp_path) -> None:
    # MAX_DEPTH-exceeding tree: NOT_VERIFIED, evaluation stops safely
    expr: dict = {"type": "file_exists", "path": "x"}
    for _ in range(MAX_DEPTH + 2):
        expr = {"all": [expr]}
    gp = parse_predicate(expr)
    r = evaluate_goal_predicate(gp, ctx(tmp_path))
    assert r.verdict is VerificationOutcome.NOT_VERIFIED
    # a 20-child group is rejected structurally by the contract
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        parse_predicate({"all": [{"type": "file_exists", "path": f"p{i}"} for i in range(20)]})


def test_evaluation_budget_bounded(tmp_path) -> None:
    for i in range(5):
        (tmp_path / f"q{i}").write_text("x", encoding="utf-8")
    c = ctx(tmp_path)
    c.budget_items = 2
    expr = {"all": [{"type": "file_exists", "path": f"q{i}"} for i in range(5)]}
    r = evaluate_goal_predicate(parse_predicate(expr), c)
    assert r.verdict is VerificationOutcome.NOT_VERIFIED  # exhausted, not faked


def test_supported_set_matches_documentation() -> None:
    assert {"file_exists", "file_content_equals", "file_contains", "file_sha256"} <= SUPPORTED_TYPES
    assert "exit_code_equals" in SUPPORTED_TYPES
