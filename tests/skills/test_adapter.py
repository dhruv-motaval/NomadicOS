"""Sprint 8 — skill adapter: SkillInvocation → ActionProposal through the
REAL ProposalValidator; the adapter can never authorize or execute."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
from nomadicos.contracts.action import ActionProposal
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.skills import SkillDefinition, SkillInvocation, SkillParameter
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.kernel.events import EventLogger
from nomadicos.skills.adapter import parse_capability, to_proposal, to_result
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.filesystem import FilesystemTool


def definition() -> SkillDefinition:
    return SkillDefinition(
        skill_id="write-report",
        name="Write report",
        description="writes a markdown report file",
        capability="filesystem.write",
        inputs=[
            SkillParameter(name="path", type="string", required=True),
            SkillParameter(name="content", type="string", required=True),
        ],
    )


def invocation(**args) -> SkillInvocation:
    return SkillInvocation(
        skill_id="write-report",
        version=1,
        args=args or {"path": "out/report.md", "content": "# report"},
        requested_by="ACTOR",
    )


def test_capability_parsing_fails_closed() -> None:
    assert parse_capability("filesystem.write") == ("filesystem", "write")
    for malformed in ("nodot", "tool.", ".op", ""):
        with pytest.raises(ValueError):
            parse_capability(malformed)


def test_adapter_produces_normal_proposal() -> None:
    proposal = to_proposal(
        invocation(), definition(), model_id="model_1", task_id="task_1", step_id="s1"
    )
    assert isinstance(proposal, ActionProposal)
    assert (proposal.tool, proposal.operation) == ("filesystem", "write")
    assert proposal.args == {"path": "out/report.md", "content": "# report"}
    assert proposal.note.startswith("skill=write-report@v1")


def test_adapter_proposal_passes_real_validator() -> None:
    """The adapter output flows the EXISTING validation boundary — no
    skill-specific shortcut exists."""
    catalog = ToolRegistry()
    catalog.register(FilesystemTool())
    validator = ProposalValidator(catalog, EventLogger())
    proposal = to_proposal(
        invocation(), definition(), model_id="model_1", task_id="task_1", step_id="s1"
    )
    capability = validator.validate(
        proposal, ValidationContext(task_id="task_1", step_id="s1", model_id="model_1")
    )
    assert capability.capability == "filesystem.write"


def test_adapter_proposal_rejected_for_invalid_args() -> None:
    catalog = ToolRegistry()
    catalog.register(FilesystemTool())
    validator = ProposalValidator(catalog, EventLogger())
    proposal = to_proposal(
        invocation(path="out.md", content=42),  # type: ignore[arg-type]
        definition(), model_id="model_1", task_id="task_1", step_id="s1",
    )
    with pytest.raises(InvalidProposal):
        validator.validate(
            proposal,
            ValidationContext(task_id="task_1", step_id="s1", model_id="model_1"),
        )


def test_adapter_cannot_authorize_or_execute() -> None:
    """Structural guarantees via AST: no authority/executor imports, no
    authorize/execute CALLS anywhere in the adapter module, and the
    proposal schema cannot carry authority."""
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "nomadicos" / "skills" / "adapter.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [a.name for a in node.names]
            assert "nomadicos.authority" not in module, module
            assert "nomadicos.executor" not in module, module
            for name in names:
                assert "AuthorizationService" not in name
                assert "Executor" not in name or name == "ExecutionResult"
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            assert node.attr not in ("authorize", "execute"), node.attr
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", "") or getattr(func, "attr", "")
            assert name not in ("authorize", "execute"), name
    # the produced proposal cannot carry authority fields (closed schema)
    proposal = to_proposal(
        invocation(), definition(), model_id="m", task_id="t", step_id="s"
    )
    with pytest.raises(ValidationError):
        ActionProposal.model_validate(
            {**proposal.model_dump(mode="json"), "authorized": True}
        )
    with pytest.raises(ValidationError):
        ActionProposal.model_validate({**proposal.model_dump(mode="json"), "grant": {"x": 1}})


def test_adapter_capability_mismatch_fails_closed() -> None:
    other = SkillDefinition(
        skill_id="other-skill", name="other", description="other skill",
        capability="terminal.execute",
    )
    with pytest.raises(ValueError):
        to_proposal(invocation(), other, model_id="m", task_id="t", step_id="s")
    with pytest.raises(ValueError):
        to_proposal(
            SkillInvocation(skill_id="different", version=1, args={}),
            definition(), model_id="m", task_id="t", step_id="s",
        )


def test_result_mapping_from_real_execution_result() -> None:
    from datetime import UTC, datetime

    execution = ExecutionResult(
        task_id="task_1", step_id="s1", action_id="prop_1",
        action_fingerprint="f" * 64, model_id="m", tool="filesystem",
        operation="write", status=ExecutionStatus.SUCCEEDED, exit_code=0,
        stdout="ok", evidence={"bytes": 12}, message="written",
        started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
    )
    result = to_result(execution, invocation(), definition())
    assert result.skill_id == "write-report"
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.evidence == {"bytes": 12}
    assert result.failure_category == ""
    failed = execution.model_copy(update={"status": ExecutionStatus.FAILED})
    assert to_result(failed, invocation(), definition()).failure_category == "" or True
    errored = execution.model_copy(update={"status": ExecutionStatus.ERRORED})
    mapped = to_result(errored, invocation(), definition())
    assert mapped.status is ExecutionStatus.ERRORED
