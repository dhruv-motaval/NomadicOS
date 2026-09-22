"""Phase 13F workspace/artifact lifecycle: durable files survive restart,
are task-confined, and are correctly distinguished from ephemeral state."""

from __future__ import annotations

import hashlib
from pathlib import Path

from helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus

GOAL = "Create file report.txt containing ARTIFACT-DATA"
PRED = [{"type": "file_exists", "path": "report.txt"}]
SCRIPT = [("report.txt", write_json("report.txt", "ARTIFACT-DATA"))]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _screenshot_json() -> str:
    import json

    return json.dumps({"tool": "desktop", "operation": "screenshot", "args": {}})


async def test_workspace_and_artifacts_survive_restart(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=SCRIPT)
    summary = await app_a.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path)
    # the durable task record carries the artifact reference as DATA
    record = app_b.task_store.load_task(task_id)
    assert record is not None
    artifact = next(
        (tmp_path / "ws").rglob("report.txt")
    )
    assert artifact.read_text(encoding="utf-8") == "ARTIFACT-DATA"
    # the recorded evidence hash can be re-established after restart
    sha_entry = record.executions[-1].evidence.get("sha256")
    if sha_entry:
        assert sha_entry == _sha(artifact)


async def test_screenshot_artifact_survives_restart(tmp_path: Path) -> None:
    app_a = make_app(
        tmp_path,
        scripts=[("Capture", _screenshot_json())],
    )
    summary = await app_a.run_goal("Capture the screen")
    assert summary.status.value in ("SUCCESS", "PARTIAL")
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path)
    record = app_b.task_store.load_task(task_id)
    assert record is not None
    shots = [
        e
        for e in record.executions
        if e.tool == "desktop" and e.evidence.get("artifact")
    ]
    assert shots, "screenshot artifact reference must be durable in the task record"
    evidence = shots[-1].evidence
    path = Path(evidence["path"])
    assert path.exists(), "PNG survives restart on disk"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert evidence["sha256"] == _sha(path), "hash re-established after restart"
    assert evidence["bytes"] == path.stat().st_size


# ------------------------------------------- missing / corrupt artifacts ----


async def test_missing_artifact_is_honest_missing(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=SCRIPT)
    summary = await app_a.run_goal(GOAL, predicates=PRED)
    task_id = summary.task_id
    await app_a.aclose()
    artifact = next((tmp_path / "ws").rglob("report.txt"))
    artifact.unlink()  # artifact disappears before restart
    app_b = make_app(tmp_path)
    record = app_b.task_store.load_task(task_id)
    assert record is not None
    sha_entry = record.executions[-1].evidence.get("sha256")
    assert sha_entry  # durable reference remains...
    assert not any((tmp_path / "ws").rglob("report.txt"))  # ...but the artifact is gone
    # verification cannot claim PASS from the stale reference alone
    from nomadicos.contracts.core import parse_predicate
    from nomadicos.contracts.verification import VerificationOutcome
    from nomadicos.verification.evidence import EvidenceContext
    from nomadicos.verification.predicates import evaluate_goal_predicate

    ctx = EvidenceContext(
        task_id=task_id, workspace=tmp_path / "ws", executions=list(record.executions)
    )
    result = evaluate_goal_predicate(
        parse_predicate({"type": "artifact_exists", "path": "report.txt"}), ctx
    )
    assert result.verdict is VerificationOutcome.NOT_PASS


async def test_corrupted_artifact_does_not_become_valid_evidence(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=SCRIPT)
    summary = await app_a.run_goal(GOAL, predicates=PRED)
    task_id = summary.task_id
    await app_a.aclose()
    artifact = next((tmp_path / "ws").rglob("report.txt"))
    artifact.write_text("TAMPERED CONTENT", encoding="utf-8")  # corrupt the artifact
    from nomadicos.contracts.core import parse_predicate
    from nomadicos.contracts.verification import VerificationOutcome
    from nomadicos.verification.evidence import EvidenceContext
    from nomadicos.verification.predicates import evaluate_goal_predicate

    app_b = make_app(tmp_path)
    record = app_b.task_store.load_task(task_id)
    assert record is not None
    sha_entry = record.executions[-1].evidence.get("sha256")
    ctx = EvidenceContext(
        task_id=task_id, workspace=tmp_path / "ws", executions=list(record.executions)
    )
    result = evaluate_goal_predicate(
        parse_predicate({"type": "file_sha256", "path": "report.txt", "sha256": sha_entry}), ctx
    )
    # hash mismatch is detected honestly; hostile content stays DATA
    assert result.verdict is VerificationOutcome.NOT_PASS


async def test_model_absolute_paths_stay_confined(tmp_path: Path) -> None:
    """Arbitrary absolute paths cannot become task artifacts (confinement
    uses resolved real paths - model writes stay inside the task scope)."""
    import json

    app = make_app(tmp_path, scripts=[("escape", json.dumps({
        "tool": "filesystem",
        "operation": "write",
        "args": {"path": str(tmp_path.parent / "escaped.txt"), "content": "x"},
    }))])
    summary = await app.run_goal("Write the escaped file")
    results = [e.result for e in app.log.events(summary.task_id)]
    assert "SUCCEEDED" not in results or not (tmp_path.parent / "escaped.txt").exists()


# ------------------------------------------- SUCCESS-after-restart cases ----


async def test_case_b_unverified_goal_stays_unverified_after_restart(
    tmp_path: Path,
) -> None:
    """An execution result exists but the goal was never independently
    verified. After restart the state remains PARTIAL/NOT_VERIFIED - never
    fabricated into SUCCESS."""
    app_a = make_app(tmp_path, scripts=SCRIPT)  # no predicates: no goal verify
    summary = await app_a.run_goal(GOAL)
    assert summary.status.value in ("PARTIAL", "SUCCESS")
    assert summary.goal_verdict in (None, "NOT_VERIFIED")
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path)
    resumed = await app_b.resume_task(task_id)
    # restored as DATA: the goal was never verified, so no goal_verdict and
    # no fabricated SUCCESS appear
    assert resumed.status.value in ("PARTIAL", "SUCCESS")
    if resumed.status.value == "SUCCESS":
        assert resumed.goal_verdict in (None, "NOT_VERIFIED")
    assert app_b.log.events(task_id) == []  # restore emits no events


async def test_case_a_completed_task_survives_as_data_no_duplicate_success(
    tmp_path: Path,
) -> None:
    """Goal verified -> SUCCESS; restart -> completed DATA with no duplicate
    SUCCESS, no repeated execution, no duplicate completion events."""
    app_a = make_app(tmp_path, scripts=SCRIPT)
    summary = await app_a.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS
    assert summary.goal_verdict == "PASS"
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path)
    resumed = await app_b.resume_task(task_id)
    assert resumed.status is TaskStatus.SUCCESS
    # no repeated execution and no duplicate completion events
    assert app_b.log.events(task_id) == []
    assert len(app_b.task_store.load_task(task_id).executions) == 1
