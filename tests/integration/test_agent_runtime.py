"""Final integration: full pipeline end-to-end (BP §78 milestone, §185 loop).

Input (CLI) → Security Gate → model propose → Tool Gateway → verify →
experience → truthful report. Runs entirely on fakes (ADR-0024).
"""

import json

import pytest

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.agent.selector import HardwareConstraints
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.core.runtime import Runtime
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.experience.store import InMemoryExperienceStore
from nomadicos.models.base import ModelStatus
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.manager import ModelManager
from nomadicos.security.budgets import TaskBudget
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway


def proposal_model(responses: list[str]) -> FakeLocalModel:
    """Scripted model emitting structured tool-call JSON (BP §86)."""
    model = FakeLocalModel("fake/planner")
    model._responses = list(responses)  # replace the default "ok" queue
    return model


def build_runtime(tmp_path, model: FakeLocalModel) -> AgentRuntime:
    policy = PolicyEngine()
    policy.load_file(_policy(tmp_path))
    gate = SecurityGate(policy, PermissionEngine(), FakeAuditSink())
    gateway = ToolGateway(gate, FakeAuditSink())
    gateway.register(FilesystemTool(workspace_root=str(tmp_path)))
    manager = ModelManager()
    manager.register(model, status=ModelStatus.ENABLED)
    tracker = ModelPerformanceTracker()
    return AgentRuntime(
        selector=__import__(
            "nomadicos.agent.selector", fromlist=["ModelSelector"]
        ).ModelSelector(
            manager, tracker, hardware=HardwareConstraints(ram_mb=8192)
        ),
        manager=manager,
        gateway=gateway,
        audit_sink=FakeAuditSink(),
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(),
        budget=TaskBudget(max_steps=4),
    )


def _policy(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        'version: "1.0.0"\n'
        "owner:\n"
        "  autonomy_level: assisted\n"
        "  tools:\n"
        "    - id: filesystem.rw\n"
        "      tool: filesystem\n"
        "      risk: medium\n"
        "      default_decision: allow\n"
        "  external_network:\n"
        "    allow_public_get: true\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def identity() -> SubjectIdentity:
    return SubjectIdentity(user_id="owner", session_id="s-1", task_id="t-1")


async def test_milestone_write_and_verify(tmp_path, identity) -> None:
    """BP §78: task → tools → verify → truthful report (write file milestone)."""
    model = proposal_model(
        [
            json.dumps(
                {
                    "tool": "filesystem",
                    "arguments": {
                        "action": "write",
                        "path": str(tmp_path / "report.txt"),
                        "content": "NomadicOS was here",
                    },
                    "finished": False,
                }
            ),
            json.dumps({"finished": True}),
        ]
    )
    runtime = build_runtime(tmp_path, model)
    report = await runtime.execute_task(
        "Write a report file", identity, max_steps=4
    )
    print("DEBUG:", report.status, report.completed, report.failed)
    assert report.status is TaskStatus.SUCCESS
    assert any("filesystem" in c for c in report.completed)
    assert (tmp_path / "report.txt").read_text() == "NomadicOS was here"
    assert report.experience_id is not None


async def test_report_truthfulness_on_failure(tmp_path, identity) -> None:
    """BP §69/§180: failures are reported, never claimed as success."""
    model = proposal_model(
        [
            json.dumps({"tool": "nonexistent.tool", "arguments": {}, "finished": False}),
            json.dumps({"finished": True}),
        ]
    )
    runtime = build_runtime(tmp_path, model)
    report = await runtime.execute_task("Impossible task", identity, max_steps=3)
    # The model's invalid tool call is refused (unknown tool ⇒ fail closed, BP §85).
    # The model then declared "finished" with zero completed steps and one recorded
    # failure ⇒ PARTIALLY_COMPLETED is the truthful status (BP §181: never "Done").
    assert report.status is TaskStatus.PARTIALLY_COMPLETED
    assert report.completed == []
    assert report.failed  # failure recorded, not hidden


async def test_invalid_model_proposal_stops_gracefully(tmp_path, identity) -> None:
    """BP §86: raw free-form text is never executed."""
    model = proposal_model(["This is not JSON at all — just chat."])
    runtime = build_runtime(tmp_path, model)
    report = await runtime.execute_task("Do a thing", identity, max_steps=2)
    assert report.status is TaskStatus.SUCCESS  # graceful stop, no execution


# ------------------------------------------------------------------ CLI + Runtime


def test_runtime_status_line() -> None:
    runtime = Runtime()
    line = runtime.status_line()
    assert "NomadicOS" in line
    assert "filesystem" in line and "terminal" in line


async def test_runtime_emergency_stop_blocks_goal() -> None:
    runtime = Runtime()
    runtime.emergency_stop()
    report = await runtime.run_goal("anything")
    assert report.status is TaskStatus.CANCELLED
    assert "emergency stop" in report.failed[0]


def test_cli_parser_accepts_task_create() -> None:
    from nomadicos.cli import build_parser

    args = build_parser().parse_args(["task", "create", "Run the tests"])
    assert args.command == "task"
    assert args.task_command == "create"
    assert args.goal == "Run the tests"


def test_cli_parser_status_and_stop() -> None:
    from nomadicos.cli import build_parser

    assert build_parser().parse_args(["status"]).command == "status"
    assert build_parser().parse_args(["stop"]).command == "stop"
