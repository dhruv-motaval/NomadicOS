"""Sprint 8 — skill contracts: bounded, closed, code-free, secret-free."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.contracts.skills import (
    SkillDefinition,
    SkillEvidence,
    SkillInvocation,
    SkillParameter,
    SkillResult,
    SkillRisk,
    contains_secret,
)


def valid_skill(**overrides) -> SkillDefinition:
    base = dict(
        skill_id="write-report",
        name="Write report",
        description="writes a markdown report file",
        capability="filesystem.write",
        inputs=[
            SkillParameter(name="path", type="string", required=True),
            SkillParameter(name="content", type="string", required=True),
        ],
    )
    base.update(overrides)
    return SkillDefinition(**base)


def test_valid_skill_accepts() -> None:
    skill = valid_skill()
    assert skill.version == 1 and skill.risk is SkillRisk.LOW and skill.enabled


def test_skill_id_and_capability_formats_fail_closed() -> None:
    with pytest.raises(ValidationError):
        valid_skill(skill_id="Bad Id")
    with pytest.raises(ValidationError):
        valid_skill(skill_id="1starts-digit")
    with pytest.raises(ValidationError):
        valid_skill(capability="no-dot")
    with pytest.raises(ValidationError):
        valid_skill(capability="tool.")
    with pytest.raises(ValidationError):
        valid_skill(capability=".op")


def test_skill_contract_is_closed() -> None:
    dump = valid_skill().model_dump(mode="json")
    with pytest.raises(ValidationError):
        SkillDefinition.model_validate({**dump, "code": "import os"})
    with pytest.raises(ValidationError):
        SkillDefinition.model_validate({**dump, "authorized": True})
    with pytest.raises(ValidationError):
        SkillDefinition.model_validate({**dump, "executable": True})


def test_skill_fields_are_bounded() -> None:
    with pytest.raises(ValidationError):
        valid_skill(skill_id="x" * 65)
    with pytest.raises(ValidationError):
        valid_skill(description="d" * 501)
    with pytest.raises(ValidationError):
        valid_skill(inputs=[SkillParameter(name=f"p{i}", type="string") for i in range(17)])
    with pytest.raises(ValidationError):
        valid_skill(prerequisites=[f"t.op{i}" for i in range(9)])
    with pytest.raises(ValidationError):
        valid_skill(constraints=["c" * 201])


def test_skills_cannot_contain_executable_code() -> None:
    # the closed schema has NO field a callable could ride on; arbitrary
    # extras are structurally rejected
    with pytest.raises(ValidationError):
        valid_skill(callback=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        SkillParameter(name="p", type="string", code="os.system('x')")  # type: ignore[call-arg]


def test_skill_definitions_reject_secrets() -> None:
    with pytest.raises(ValidationError):
        valid_skill(description="connects using password=hunter2")
    with pytest.raises(ValidationError):
        valid_skill(name="token: abcdef123456 holder")
    with pytest.raises(ValidationError):
        SkillParameter(name="p", type="string", description="api_key: zzz")
    with pytest.raises(ValidationError):
        SkillInvocation(
            skill_id="write-report", version=1, args={"path": "token=abc123"}
        )
    assert contains_secret("password=hunter2") is True
    assert contains_secret("plain text") is False


def test_invocation_bounded_and_authority_free() -> None:
    with pytest.raises(ValidationError):
        SkillInvocation(
            skill_id="write-report", version=1,
            args={f"k{i}": "v" for i in range(33)},
        )
    with pytest.raises(ValidationError):
        SkillInvocation(skill_id="write-report", version=1, args={"nested": {"a": 1}})
    with pytest.raises(ValidationError):
        SkillInvocation(skill_id="write-report", version=1, args={"bypass": True})
    with pytest.raises(ValidationError):
        SkillInvocation.model_validate(
            {"skill_id": "write-report", "version": 1, "authorized": True}
        )
    # ids are unique and prefixed
    a = SkillInvocation(skill_id="write-report", version=1)
    b = SkillInvocation(skill_id="write-report", version=1)
    assert a.invocation_id.startswith("task_") and a.invocation_id != b.invocation_id


def test_skill_version_handling() -> None:
    assert valid_skill(version=1).version == 1
    with pytest.raises(ValidationError):
        valid_skill(version=0)
    with pytest.raises(ValidationError):
        valid_skill(version=10_000)


def test_skill_evidence_never_invents_metrics() -> None:
    with pytest.raises(ValidationError):
        SkillEvidence(samples=0, success_rate=0.9)
    with pytest.raises(ValidationError):
        SkillEvidence(samples=2, successes=1, failures=0)
    with pytest.raises(ValidationError):
        SkillEvidence(samples=3, successes=2, failures=1)  # counts must sum


def test_skill_result_mapping_shape() -> None:
    result = SkillResult(
        skill_id="write-report", version=1, invocation_id="task_1",
        status=ExecutionStatus.SUCCEEDED, evidence={"bytes": 10},
    )
    assert result.failure_category == ""
    with pytest.raises(ValidationError):
        SkillResult(
            skill_id="write-report", version=1, invocation_id="task_1",
            status=ExecutionStatus.SUCCEEDED, evidence={f"k{i}": 1 for i in range(17)},
        )
    with pytest.raises(ValidationError):
        SkillResult(
            skill_id="write-report", version=1, invocation_id="task_1",
            status=ExecutionStatus.SUCCEEDED, evidence={"secret": "api_key: zzz"},
        )
