"""Sprint 8 — skill evaluation: deterministic honest measurement feeding
retrieval evidence (Sprint 7-style measured-not-declared quality)."""

from __future__ import annotations

import pytest

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.contracts.skills import (
    SkillDefinition,
    SkillEvaluationRecord,
    SkillEvidence,
    SkillParameter,
    SkillResult,
)
from nomadicos.skills.evaluation import evaluate, record_outcome


def definition(**evidence_overrides) -> SkillDefinition:
    evidence = SkillEvidence()
    if evidence_overrides:
        evidence = SkillEvidence(**evidence_overrides)
    return SkillDefinition(
        skill_id="write-report",
        name="Write report",
        description="writes a markdown report file",
        capability="filesystem.write",
        inputs=[SkillParameter(name="path", type="string", required=True)],
        evidence=evidence,
    )


def result(status: ExecutionStatus, *, category: str = "") -> SkillResult:
    return SkillResult(
        skill_id="write-report",
        version=1,
        invocation_id="task_x",
        status=status,
        failure_category=category,
    )


def test_record_outcome_accumulates_deterministically() -> None:
    skill = definition()
    after_success = record_outcome(skill, result(ExecutionStatus.SUCCEEDED))
    assert after_success.evidence.samples == 1
    assert after_success.evidence.successes == 1
    assert after_success.evidence.success_rate == 1.0
    after_fail = record_outcome(after_success, result(ExecutionStatus.FAILED))
    assert after_fail.evidence.samples == 2
    assert after_fail.evidence.success_rate == 0.5
    # deterministic: same outcomes → same evidence
    replay = record_outcome(
        record_outcome(definition(), result(ExecutionStatus.SUCCEEDED)),
        result(ExecutionStatus.FAILED),
    )
    assert replay.evidence == after_fail.evidence
    # the input definition is never mutated
    assert skill.evidence.samples == 0


def test_evaluation_is_bounded() -> None:
    calls = []

    def run(defn: SkillDefinition) -> SkillResult:
        calls.append(1)
        return result(ExecutionStatus.SUCCEEDED)

    record = evaluate(definition(), run, cases=1000)
    assert record.cases == 32  # hard bound (MAX_EVALUATION_CASES)
    assert record.success_rate == 1.0


def test_evaluation_counts_honestly() -> None:
    outcomes = iter(
        [
            result(ExecutionStatus.SUCCEEDED),
            result(ExecutionStatus.FAILED),
            result(ExecutionStatus.ERRORED, category="VALIDATION"),
        ]
    )

    def run(defn: SkillDefinition) -> SkillResult:
        return next(outcomes)

    record = evaluate(definition(), run, cases=3)
    assert record.cases == 3
    assert record.successes == 1
    assert record.failures == 1
    assert record.validation_failures == 1
    assert record.success_rate == pytest.approx(1 / 3, abs=1e-4)


def test_evaluation_fail_safe_on_callback_error() -> None:
    def run(defn: SkillDefinition) -> SkillResult:
        raise RuntimeError("runtime exploded")

    record = evaluate(definition(), run, cases=2)
    assert record.cases == 2
    assert record.failures == 2  # honest failure, never skipped/invented
    assert record.success_rate == 0.0


def test_evaluation_record_validation() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillEvaluationRecord(skill_id="s", version=1, cases=2, successes=1, failures=0)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillEvaluationRecord(skill_id="s", version=1, cases=3, successes=3, failures=0)
    # measured evaluation must carry its rate
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillEvaluationRecord(
            skill_id="s", version=1, cases=2, successes=2, failures=0, success_rate=None
        )


def test_measured_evidence_feeds_retrieval_ranking() -> None:
    """skill candidate → evaluation → measured quality → retrieval evidence."""
    from nomadicos.contracts.skills import SkillQuery
    from nomadicos.skills.registry import SkillRegistry
    from nomadicos.skills.retrieval import rank_skills

    skill = record_outcome(definition(), result(ExecutionStatus.SUCCEEDED))
    registry = SkillRegistry()
    registry.register(skill)
    query = SkillQuery(capability="filesystem.write", provided_args=["path"])
    hit = rank_skills(registry, query).matches[0]
    assert hit.measured is True
    assert hit.score > 0.5  # measured quality lifts the score above cold start
    assert "success_rate=1.0000" in hit.reasons[1]
