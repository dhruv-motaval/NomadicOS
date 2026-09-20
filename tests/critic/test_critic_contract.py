"""Critic contract/parser units (SPEC §10.2-§10.15, §10.28)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.agents.critic import (
    CriticRequest,
    _claim_to_report,
    critic_feedback_lines,
    implementation_revision,
    parse_critic_output,
)
from nomadicos.contracts.core import Goal
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import CriticDecision, CriticReport
from nomadicos.kernel.errors import InvalidProposal


def good_request(**over) -> CriticRequest:
    base = dict(
        task_id="t1",
        goal=Goal(objective="fix it"),
        iteration=1,
        implementation_revision="rev-abc",
        changes=[{"tool": "filesystem", "op": "write", "path": "a.py"}],
        test_results=[
            {"command": "pytest", "exit_code": 0, "stdout_tail": "1 passed", "stderr_tail": ""}
        ],
        verifications=[],
        failures=[],
        previous_feedback=None,
        tests_passed=True,
        goal_verified=False,
    )
    base.update(over)
    return CriticRequest(**base)


# ------------------------------------------------------------ parsing -----


def test_valid_critic_json_parses() -> None:
    fields = parse_critic_output(
        '{"decision": "IMPROVE", "score": 8.7,'
        ' "critical_issues": [], "major_issues": ["Missing regression test"],'
        ' "minor_issues": [], "suggestions": ["Add nested-input coverage"],'
        ' "required_tests": ["test_nested_input"], "evidence_refs": ["exec:a"]}'
    )
    assert fields["decision"] == "IMPROVE"
    assert fields["major_issues"] == ["Missing regression test"]
    assert fields["required_tests"] == ["test_nested_input"]


def test_missing_fields_and_wrong_types_rejected() -> None:
    for bad in [
        '{"decision": "IMPROVE", "score": "eight"}',
        '{"score": 7}',
        '{"decision": "IMPROVE", "major_issues": "not a list"}',
        '{"decision": "IMPROVE", "required_tests": [1, 2]}',
        "{}",
        "[1]",
        "",
        "great work!",
    ]:
        with pytest.raises(InvalidProposal):
            parse_critic_output(bad)


def test_score_range_enforced() -> None:
    for score in ("10.5", "-0.1", "true"):
        with pytest.raises(InvalidProposal):
            parse_critic_output(f'{{"decision": "ACCEPT", "score": {score}}}')
    assert parse_critic_output('{"decision": "ACCEPT", "score": 10}')["score"] == 10


def test_unknown_decision_rejected() -> None:
    with pytest.raises(InvalidProposal):
        parse_critic_output('{"decision": " LGTM ", "score": 9}')


def test_authority_and_completion_claims_rejected_deep() -> None:
    for field in (
        "verified",
        "goal_complete",
        "owner_approved",
        "tests_passed",
        "goal_verified",
        "bypass",
    ):
        raw = (
            '{"decision": "ACCEPT", "score": 9.9,'
            f' "major_issues": [{{"nested": [{{"{field}": true}}]}}]}}'
            if field in ("verified", "goal_complete")
            else f'{{"decision": "ACCEPT", "{field}": true}}'
        )
        with pytest.raises(InvalidProposal):
            parse_critic_output(raw)


# ------------------------------------------------ suppression of ACCEPT ---


def test_claimed_accept_without_goal_evidence_is_downgraded() -> None:
    fields = parse_critic_output('{"decision": "ACCEPT", "score": 9.8}')
    result = _claim_to_report(fields, good_request(goal_verified=False), model_id="critic-1")
    assert result.decision is CriticDecision.IMPROVE
    assert result.report is not None
    assert result.report.accept_suppressed is True
    assert any("goal verifier" in c for c in result.report.critical_issues)
    # the model claimed acceptance; the system's evidence says otherwise
    assert result.report.model_id == "critic-1"


def test_accept_valid_when_full_engineering_evidence_present() -> None:
    fields = parse_critic_output('{"decision": "ACCEPT", "score": 9.8}')
    result = _claim_to_report(
        fields, good_request(tests_passed=True, goal_verified=True), model_id="critic-1"
    )
    assert result.decision is CriticDecision.ACCEPT
    assert result.report is not None and result.report.accept_suppressed is False


def test_reject_and_malformed_model_paths() -> None:
    fields = parse_critic_output('{"decision": "REJECT", "critical_issues": ["injection sink"]}')
    result = _claim_to_report(fields, good_request(), model_id="critic-1")
    assert result.decision is CriticDecision.REJECT
    with pytest.raises(ValidationError):
        CriticReport(model_id="m", task_id="t", decision=CriticDecision.NOT_EVALUATED, score=5.0)


# --------------------------------------------------------- correlation ----


def test_implementation_revision_tracks_executed_state() -> None:
    def ex(fp: str, sha: str) -> ExecutionResult:
        return ExecutionResult(
            task_id="t",
            step_id="s",
            action_id="a",
            action_fingerprint=fp,
            model_id="m",
            tool="filesystem",
            operation="write",
            status=ExecutionStatus.SUCCEEDED,
            evidence={"path": "a", "sha256": sha},
        )

    e1 = ex("f1", "sha-1")
    assert implementation_revision([e1]) == implementation_revision([e1])
    assert implementation_revision([e1]) != implementation_revision([e1, ex("f2", "sha-2")])


def test_feedback_lines_stale_marker_and_rendering() -> None:
    fb = {
        "for_revision": "rev-old",
        "decision": "IMPROVE",
        "critical_issues": [],
        "major_issues": ["missing test"],
        "minor_issues": [],
        "suggestions": ["cover nested"],
        "required_tests": ["test_nested"],
    }
    text = critic_feedback_lines(fb)
    assert "[MAJOR] missing test" in text
    assert "[suggest] cover nested" in text
    assert "[requires test" in text
    assert critic_feedback_lines(None) == ""
