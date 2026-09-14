"""Phase 10 tests: EvaluationEngine + RunRecord scoring (BP §100, §144-146, §366)."""

import pytest

from nomadicos.core.errors import VerificationFailed
from nomadicos.evaluation.base import Evidence
from nomadicos.evaluation.deterministic import FilesystemVerifier, TerminalVerifier
from nomadicos.evaluation.engine import EvaluationEngine, RunRecord
from nomadicos.evaluation.model_eval import ModelPerformanceTracker


@pytest.fixture()
def engine() -> EvaluationEngine:
    return EvaluationEngine(
        {
            "filesystem": FilesystemVerifier(),
            "terminal": TerminalVerifier(expected_exit_code=0),
        }
    )


async def test_verify_unknown_kind_fails_closed(engine: EvaluationEngine) -> None:
    """BP §85: no verifier for a kind ⇒ refuse, never assume success."""
    with pytest.raises(VerificationFailed, match="no verifier"):
        await engine.verify("mystery", Evidence(kind="mystery", facts={}))


async def test_evaluate_run_all_checks_pass(engine: EvaluationEngine) -> None:
    record = await engine.evaluate_run(
        task_id="t-1",
        run_id="r-1",
        evidence_bundles=[
            (
                "filesystem",
                Evidence(
                    kind="filesystem",
                    facts={
                        "path": "a.txt",
                        "exists": True,
                        "hash_before": "h1",
                        "hash_after": "h2",
                    },
                ),
            ),
            ("terminal", Evidence(kind="terminal", facts={"exit_code": 0, "stdout": "42 passed"})),
        ],
        steps_taken=4,
        retries_used=0,
        duration_seconds=12.5,
    )
    assert record.verified is True
    assert record.checks_total == 4
    assert record.checks_passed == 4
    assert record.score == 10.0


async def test_evaluate_run_partial_failure_limits_score(engine: EvaluationEngine) -> None:
    record = await engine.evaluate_run(
        task_id="t-1",
        run_id="r-2",
        evidence_bundles=[
            ("terminal", Evidence(kind="terminal", facts={"exit_code": 1, "stdout": "boom"})),
        ],
    )
    assert record.verified is False
    assert record.score <= 4.0  # unverified runs never score high (BP §366)


async def test_empty_evidence_scores_zero(engine: EvaluationEngine) -> None:
    record = await engine.evaluate_run(task_id="t", run_id="r", evidence_bundles=[])
    assert record.score == 0.0
    assert record.verdict_summary == "no evidence collected"


def test_run_record_score_bounds() -> None:
    record = RunRecord(
        task_id="t",
        run_id="r",
        verified=False,
        verdict_summary="",
        checks_passed=3,
        checks_total=4,
        steps_taken=1,
        retries_used=0,
        duration_seconds=1.0,
    )
    assert 0.0 <= record.score <= 4.0


# ------------------------------------------------------- model performance


def test_tracker_records_and_scores() -> None:
    tracker = ModelPerformanceTracker()
    for _ in range(9):
        tracker.record(
            model_id="llama/3b",
            task_family="coding",
            success=True,
            duration_seconds=1.2,
            verified=True,
        )
    tracker.record(
        model_id="llama/3b",
        task_family="coding",
        success=False,
        duration_seconds=2.0,
        verified=False,
    )
    stats = tracker._stats[("llama/3b", "coding")]
    assert stats["attempts"] == 10
    assert stats["successes"] == 9
    assert stats["verified_successes"] == 9
    score = tracker.quality_score("llama/3b", "coding")
    assert 0.0 < score <= 10.0


def test_tracker_unknown_model_scores_zero() -> None:
    tracker = ModelPerformanceTracker()
    assert tracker.quality_score("missing/model", "coding") == 0.0
    assert tracker.median_latency("missing/model", "coding") is None


def test_tracker_task_family_separation() -> None:
    tracker = ModelPerformanceTracker()
    tracker.record(
        model_id="m", task_family="coding", success=True, duration_seconds=1.0, verified=True
    )
    tracker.record(
        model_id="m",
        task_family="research",
        success=False,
        duration_seconds=2.0,
        verified=False,
    )
    assert tracker.quality_score("m", "coding") > 0.0
    assert tracker.quality_score("m", "research") == 0.0
