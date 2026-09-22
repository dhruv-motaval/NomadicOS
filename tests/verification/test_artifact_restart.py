"""Phase 13F verification-after-restart: persisted evidence is not verified
truth. The verifier re-reads live reality; stale references never PASS."""

from __future__ import annotations

from pathlib import Path

from nomadicos.contracts.core import parse_predicate
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.verification.evidence import EvidenceContext
from nomadicos.verification.predicates import evaluate_goal_predicate

TASK = "task_ev"


def _write_exec(path: str, sha: str | None = None) -> ExecutionResult:
    evidence = {"path": path, "sha256": sha} if sha else {"path": path}
    return ExecutionResult(
        task_id=TASK,
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="filesystem",
        operation="write",
        status=ExecutionStatus.SUCCEEDED,
        evidence=evidence,
    )


def _verdict(expr: dict, ctx: EvidenceContext):
    return evaluate_goal_predicate(parse_predicate(expr), ctx)


def test_case_c_deleted_artifact_prevents_fabricated_pass(tmp_path: Path) -> None:
    """Evidence existed before the crash; the artifact disappears before
    restart. The verifier cannot claim PASS from stale durable evidence."""
    (tmp_path / "report.txt").write_text("DATA", encoding="utf-8")  # existed pre-crash
    exec = _write_exec("report.txt", sha="a" * 64)
    (tmp_path / "report.txt").unlink()  # artifact disappears before restart
    ctx = EvidenceContext(task_id=TASK, workspace=tmp_path, executions=[exec])
    assert (
        _verdict({"type": "artifact_exists", "path": "report.txt"}, ctx).verdict
        is VerificationOutcome.NOT_PASS
    )
    assert (
        _verdict({"type": "file_sha256", "path": "report.txt", "sha256": "a" * 64}, ctx).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_case_c_missing_reference_stays_not_verified(tmp_path: Path) -> None:
    exec = _write_exec("ghost.txt")
    ctx = EvidenceContext(task_id=TASK, workspace=tmp_path, executions=[exec])
    assert (
        _verdict({"type": "artifact_exists", "path": "ghost.txt"}, ctx).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_case_d_artifact_remains_and_is_reread(tmp_path: Path) -> None:
    data = "REAL-DATA"
    (tmp_path / "out.txt").write_text(data, encoding="utf-8")
    import hashlib

    sha = hashlib.sha256((tmp_path / "out.txt").read_bytes()).hexdigest()
    ctx = EvidenceContext(
        task_id=TASK, workspace=tmp_path, executions=[_write_exec("out.txt", sha)]
    )
    assert (
        _verdict({"type": "artifact_exists", "path": "out.txt"}, ctx).verdict
        is VerificationOutcome.PASS
    )
    assert (
        _verdict({"type": "file_sha256", "path": "out.txt", "sha256": sha}, ctx).verdict
        is VerificationOutcome.PASS
    )
    assert (
        _verdict({"type": "file_content_equals", "path": "out.txt", "content": data}, ctx).verdict
        is VerificationOutcome.PASS
    )


def test_case_d_modified_artifact_fails_hash(tmp_path: Path) -> None:
    (tmp_path / "out.txt").write_text("ORIGINAL", encoding="utf-8")
    import hashlib

    sha = hashlib.sha256(b"ORIGINAL").hexdigest()
    exec = _write_exec("out.txt", sha)
    (tmp_path / "out.txt").write_text("MODIFIED", encoding="utf-8")  # modified after
    ctx = EvidenceContext(task_id=TASK, workspace=tmp_path, executions=[exec])
    assert (
        _verdict({"type": "file_sha256", "path": "out.txt", "sha256": sha}, ctx).verdict
        is VerificationOutcome.NOT_PASS
    )


def test_case_b_execution_without_verification_stays_unverified(tmp_path: Path) -> None:
    """An execution result exists but the goal was never verified - restored
    state remains NOT_VERIFIED; no fabricated SUCCESS."""
    click = ExecutionResult(
        task_id=TASK,
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="desktop",
        operation="mouse_click",
        status=ExecutionStatus.SUCCEEDED,
        evidence={"x": 1, "y": 2},
    )
    ctx = EvidenceContext(task_id=TASK, workspace=tmp_path, executions=[click])
    assert (
        _verdict({"type": "window_present", "title": "notepad"}, ctx).verdict
        is VerificationOutcome.NOT_VERIFIED
    )


def test_restored_evidence_keeps_task_correlation(tmp_path: Path) -> None:
    """A restored evidence record must still satisfy task identity: task B
    cannot verify from task A's restored executions. In production each
    task has its own workspace scope, so task A's artifacts are not even
    resolvable from task B's context."""
    other = ExecutionResult(
        task_id="task_OTHER",
        step_id="step_1",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="filesystem",
        operation="write",
        status=ExecutionStatus.SUCCEEDED,
        evidence={"path": "x.txt"},
    )
    (tmp_path / "x.txt").write_text("DATA", encoding="utf-8")  # task A's artifact
    ctx = EvidenceContext(task_id=TASK, workspace=tmp_path / "task_B_scope", executions=[other])
    assert (
        _verdict({"type": "artifact_exists", "path": "x.txt"}, ctx).verdict
        is VerificationOutcome.NOT_PASS
    )
    # ...and the execution correlation itself is rejected cross-task
    assert (
        _verdict({"type": "exit_code_equals", "command": "whatever", "code": 0}, ctx).verdict
        is VerificationOutcome.NOT_VERIFIED
    )
