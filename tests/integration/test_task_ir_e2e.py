"""STEP 2 end-to-end boundary proofs (rebuild plan tests 4, 11-15).

Nothing is asserted from reading code: policy/executor behavior is proven by
EXECUTING the real loop and inspecting real audits, real files, real reports.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.errors import ValidationError
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.experience.store import InMemoryExperienceStore
from nomadicos.models.base import ModelStatus
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.manager import ModelManager
from nomadicos.security.budgets import TaskBudget, TaskBudgetTracker
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway

IDENTITY = SubjectIdentity(user_id="owner", session_id="s-1", task_id=None)


def _policy(tmp_path) -> None:
    p = tmp_path / "owner.yaml"
    p.write_text(
        'version: "1.0.0"\nowner:\n  autonomy_level: autonomous\n  tools:\n'
        "    - id: filesystem.rw\n      tool: filesystem\n      risk: medium\n"
        "      default_decision: allow\n  external_network:\n    allow_public_get: true\n",
        encoding="utf-8",
    )


def _model(responses: list[str]) -> FakeLocalModel:
    m = FakeLocalModel("fake/planner")
    m._responses = list(responses)
    return m


def _runtime(tmp_path, model: FakeLocalModel, audit: FakeAuditSink) -> AgentRuntime:
    policy = PolicyEngine()
    _policy(tmp_path)
    policy.load_file(tmp_path / "owner.yaml")
    gate = SecurityGate(policy, PermissionEngine(), audit)
    gw = ToolGateway(gate, audit)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    gw.register(FilesystemTool(workspace_root=str(workspace)))
    mgr = ModelManager()
    mgr.register(model, status=ModelStatus.ENABLED)
    return AgentRuntime(
        selector=ModelSelector(
            mgr, ModelPerformanceTracker(), hardware=HardwareConstraints(ram_mb=8192)
        ),
        manager=mgr,
        gateway=gw,
        audit_sink=audit,
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(),
        budget=TaskBudget(max_steps=4, max_retries=2),
        workspace_root=str(workspace),
    )


def test_valid_ir_executes_produces_file_and_step_identity(tmp_path) -> None:
    audit = FakeAuditSink()
    write = json.dumps(
        {
            "tool": "filesystem",
            "arguments": {"action": "write", "path": "note.txt", "content": "IR works"},
            "finished": False,
        }
    )
    rt = _runtime(tmp_path, _model([write, '{"finished": true}']), audit)
    report = asyncio.run(rt.execute_task("Write a note", IDENTITY, max_steps=3))
    assert report.status is TaskStatus.SUCCESS and report.completed
    # The canonical IR really executed: file exists with the IR's content.
    body = (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8")
    assert "IR works" in body
    # Policy was consulted about the IR's tool with its step identity attached:
    tool_decisions = [e for e in audit.events if e.subject == "filesystem"]
    assert any(e.decision in ("ALLOW", "EXECUTED") for e in tool_decisions), tool_decisions
    done = [e for e in audit.events if e.decision == "STEP_DONE" and e.step_id]
    assert done, [e.decision for e in audit.events]
    assert done[0].step_id == "attempt-1-step-1"
    assert done[0].task_id == report.task_id


def test_raw_dict_cannot_reach_executor(tmp_path) -> None:
    rt = _runtime(tmp_path, _model([]), FakeAuditSink())
    with pytest.raises(ValidationError, match="canonical TaskAction"):
        asyncio.run(
            rt._mediated_execute(
                {"tool": "filesystem", "arguments": {}},  # deliberately NOT a TaskAction
                IDENTITY,
                TaskBudgetTracker(TaskBudget()),
            )
        )


def test_authority_field_claim_is_rejected_by_the_loop(tmp_path) -> None:
    audit = FakeAuditSink()
    sneaky = json.dumps(
        {
            "tool": "filesystem",
            "arguments": {"action": "write", "path": "x.txt", "content": "hi"},
            "allowed": True,
        }
    )
    rt = _runtime(tmp_path, _model([sneaky] * 3), audit)
    report = asyncio.run(rt.execute_task("Read x sneakily", IDENTITY, max_steps=3))
    assert report.status is TaskStatus.FAILED
    assert any("authority" in f for f in report.failed), report.failed
    assert not (tmp_path / "workspace" / "x.txt").exists()  # nothing executed
    # policy never saw this claim at all:
    allowed = [e for e in audit.events if e.decision in ("ALLOW", "ASK")]
    assert not [e for e in allowed if e.subject == "filesystem"]


def test_unknown_action_denied_at_registry_before_policy(tmp_path) -> None:
    audit = FakeAuditSink()
    ghost = json.dumps({"tool": "ghost.tool", "arguments": {}, "finished": False})
    rt = _runtime(tmp_path, _model([ghost] * 4), audit)
    report = asyncio.run(rt.execute_task("Ghost the thing", IDENTITY, max_steps=4))
    assert report.status is TaskStatus.FAILED
    assert any("ghost.tool" in f and "unknown tool" in f for f in report.failed), report.failed
    # The gate was never asked about ghost.tool (only ModelManager etc. appear):
    ghost_events = [e for e in audit.events if e.subject == "ghost.tool"]
    assert not [e for e in ghost_events if e.decision in ("ALLOW", "ASK")]


def test_malformed_output_never_finishes(tmp_path) -> None:
    rt = _runtime(tmp_path, _model(["not json at all", "{broken"]), FakeAuditSink())
    report = asyncio.run(rt.execute_task("Do a thing", IDENTITY, max_steps=2))
    assert report.status is not TaskStatus.SUCCESS
    assert report.completed == []
    assert any("unparseable" in f for f in report.failed), report.failed


def test_retry_identity_stable_task_distinct_steps(tmp_path) -> None:
    audit = FakeAuditSink()
    rt = _runtime(tmp_path, _model(["junk"] * 12), audit)
    report = asyncio.run(rt.execute_task("Forever junk", IDENTITY, max_steps=1))
    assert report.status is TaskStatus.FAILED
    step_ids = {e.step_id for e in audit.events if e.decision == "STEP_DONE" and e.step_id}
    # every step label is attempt-scoped (stable task, distinct step labels):
    assert all(str(s).startswith("attempt-") for s in step_ids), step_ids
    assert report.task_id
