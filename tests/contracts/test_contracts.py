"""Contracts: goals, actions, verification (SPEC §18-21, §27-29)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts import (
    MODEL_AUTHORITY_FIELDS,
    ActionProposal,
    AuthorizationGrant,
    AuthorizedAction,
    CapabilityRef,
    CriticDecision,
    CriticReport,
    EvidenceItem,
    ExecutionResult,
    ExecutionStatus,
    Goal,
    PlanStep,
    TaskStatus,
    VerificationLevel,
    VerificationResult,
    parse_predicate,
)
from nomadicos.kernel.errors import Failure


def test_goal_parsing_nested_predicates() -> None:
    g = Goal.from_spec(
        "Repair app and make tests pass",
        constraints=["Do not touch Project B"],
        predicates=[
            {
                "all": [
                    {"type": "tests_pass", "command": "pytest"},
                    {"type": "file_exists", "path": "a.py"},
                ]
            },
            {"any": [{"type": "process_started"}, {"type": "window_present"}]},
        ],
    )
    assert g.constraints == ["Do not touch Project B"]
    assert len(g.predicates) == 2
    assert g.predicates[0].group is not None
    assert g.predicates[0].group.children[0].predicate is not None
    assert g.predicates[0].group.children[0].predicate.field("command") == "pytest"


def test_predicate_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_predicate({"type": "x", "all": [{"type": "y"}]})
    with pytest.raises(ValueError):
        parse_predicate(["list"])
    with pytest.raises(ValueError):
        parse_predicate({"nope": []})


def test_goal_requires_objective() -> None:
    with pytest.raises(ValidationError):
        Goal(objective="   ")


def test_proposal_rejects_model_authored_authority() -> None:
    base = dict(
        task_id="task_1", step_id="step_1", model_id="m", tool="terminal", operation="execute"
    )
    with pytest.raises(ValidationError):
        ActionProposal(**base, authorized=True)  # type: ignore[call-overload]
    with pytest.raises(ValidationError):
        ActionProposal(**base, bypass_policy=True)  # type: ignore[call-overload]
    for field in MODEL_AUTHORITY_FIELDS:
        assert field.islower()


def test_fingerprint_semantic_equivalence() -> None:
    args = {"command": "python", "args": ["test.js"]}
    a = ActionProposal(
        task_id="task_1",
        step_id="s1",
        model_id="m",
        tool="terminal",
        operation="execute",
        args=args,
    )
    b = ActionProposal(
        task_id="task_1",
        step_id="s1",
        model_id="m",
        tool="terminal",
        operation="execute",
        args={"args": ["test.js"], "command": "python"},
    )
    assert a.fingerprint() == b.fingerprint()
    c = ActionProposal(
        task_id="task_1",
        step_id="s1",
        model_id="m",
        tool="terminal",
        operation="execute",
        args={"command": "node", "args": ["test.js"]},
    )
    assert c.fingerprint() != a.fingerprint()


def test_authorized_action_binds_grant_to_proposal() -> None:
    prop = ActionProposal(
        task_id="task_1",
        step_id="s1",
        model_id="m",
        tool="filesystem",
        operation="write",
        args={"path": "a.txt", "content": "x"},
    )
    grant = AuthorizationGrant(
        proposal_id=prop.id,
        fingerprint=prop.fingerprint(),
        profile="FULL_PC_AUTONOMY",
        granted_by="policy",
        reason="capability granted",
        authority_epoch=3,
    )
    aa = AuthorizedAction(proposal=prop, grant=grant)
    assert aa.proposal.operation == "write"
    tampered = grant.model_copy(update={"fingerprint": "deadbeef"})
    with pytest.raises(ValidationError):
        AuthorizedAction(proposal=prop, grant=tampered)
    other_grant = grant.model_copy(update={"proposal_id": "prop_other"})
    with pytest.raises(ValidationError):
        AuthorizedAction(proposal=prop, grant=other_grant)


def test_capability_ref_resolution() -> None:
    ref = CapabilityRef(capability="filesystem.write", resource="/tmp/x")
    assert ref.namespace == "filesystem"
    assert ref.wildcard == "filesystem.*"


def test_verification_passed_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(level=VerificationLevel.GOAL, task_id="t1", evidence=[])
    ok = VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="t1",
        evidence=[EvidenceItem(claim="file exists", observed=True, detail={"path": "a"})],
    )
    assert ok.passed
    bad = VerificationResult(
        level=VerificationLevel.STEP,
        task_id="t1",
        evidence=[EvidenceItem(claim="tests pass", observed=False)],
    )
    assert not bad.passed
    assert bad.missing == ["tests pass"]


def test_execution_result_succeeded_semantics() -> None:
    def mk(status: ExecutionStatus) -> ExecutionResult:
        return ExecutionResult(
            task_id="t1",
            step_id="s1",
            action_id="a1",
            action_fingerprint="f",
            model_id="m",
            tool="terminal",
            operation="execute",
            status=status,
        )

    assert mk(ExecutionStatus.SUCCEEDED).succeeded
    for st in (ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT, ExecutionStatus.ERRORED):
        r = mk(st)
        assert not r.succeeded


def test_execution_result_carries_failure_category() -> None:
    r = ExecutionResult(
        task_id="t1",
        step_id="s1",
        action_id="a1",
        action_fingerprint="f",
        model_id="m",
        tool="terminal",
        operation="execute",
        status=ExecutionStatus.FAILED,
        exit_code=2,
        failure=Failure.ACTION_FAILED,
    )
    assert r.failure is Failure.ACTION_FAILED


def test_critic_accept_requires_engineering_evidence() -> None:
    with pytest.raises(ValidationError):
        CriticReport(model_id="critic", task_id="t1", decision=CriticDecision.ACCEPT, score=9.1)
    with pytest.raises(ValidationError):
        CriticReport(
            model_id="critic",
            task_id="t1",
            decision=CriticDecision.ACCEPT,
            score=9.1,
            tests_passed=True,
            goal_verified=True,
            critical_issues=["segfault"],
        )
    ok = CriticReport(
        model_id="critic",
        task_id="t1",
        decision=CriticDecision.ACCEPT,
        score=9.1,
        tests_passed=True,
        goal_verified=True,
    )
    assert ok.summary()["decision"] == "ACCEPT"
    improve = CriticReport(
        model_id="critic",
        task_id="t1",
        decision=CriticDecision.IMPROVE,
        score=8.1,
        major_issues=["Missing parser regression test"],
        required_tests=["test_nested_emphasis"],
    )
    assert improve.summary()["major"] == ["Missing parser regression test"]


def test_task_status_success_is_not_default() -> None:
    assert TaskStatus.SUCCESS != "CREATED"
    plan_step = PlanStep(description="edit parser")
    assert plan_step.expected is None
