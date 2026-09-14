"""Multi-agent orchestrator tests (ADR-0030)."""

import json

import pytest

from nomadicos.agent.orchestrator import Orchestrator
from nomadicos.agent.runtime import AgentRuntime
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.core.errors import ModelFailure
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.experience.store import InMemoryExperienceStore
from nomadicos.models.base import GenerateResult
from nomadicos.security.budgets import TaskBudget
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.fake import FakeTool
from nomadicos.tools.gateway import ToolGateway


class ScriptedModel:
    """Planner model returning a scripted plan."""

    def __init__(self, plan: str | Exception) -> None:
        self._plan = plan

    async def generate(self, request):
        if isinstance(self._plan, Exception):
            raise self._plan
        return GenerateResult(text=self._plan)


def make_permissive_runtime_factory(tracker):
    """Builds real AgentRuntimes whose gate allows everything (orchestration focus)."""

    def factory(agent_id, role):
        tool = FakeTool(
            name="fake.work",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

        async def authorize_all(**kwargs):
            from nomadicos.constitution.policy_schema import RiskLevel
            from nomadicos.security.gate import Decision, SecurityDecision

            return SecurityDecision(Decision.ALLOW, "test-permissive", RiskLevel.LOW, 1, False, 0.0)

        gateway = ToolGateway(None, FakeAuditSink())
        gateway.register(tool)

        async def _authorize(*args, **kwargs):
            return authorize_all()

        gateway._gate = type(
            "Gate",
            (),
            {"authorize": staticmethod(_authorize)},
        )()
        gateway._tools = {"fake.work": tool}

        return AgentRuntime(
            selector=None,
            manager=None,
            gateway=gateway,
            audit_sink=FakeAuditSink(),
            recorder=ExperienceRecorder(InMemoryExperienceStore()),
            evaluator=EvaluationEngine(),
            budget=TaskBudget(max_steps=2, max_model_calls=4),
        )

    return factory


@pytest.fixture()
def identity() -> SubjectIdentity:
    return SubjectIdentity(user_id="owner", session_id="s-1", task_id="t-1")


async def test_plan_rejects_forward_dependency(identity) -> None:
    forward = json.dumps(
        {
            "subtasks": [
                {"id": "s1", "description": "a", "task_type": "research", "depends_on": ["s2"]},
                {"id": "s2", "description": "b", "task_type": "research", "depends_on": []},
            ]
        }
    )
    orchestrator = Orchestrator(ScriptedModel(forward), FakeAuditSink(), lambda aid, role: None)
    with pytest.raises(ModelFailure, match="invalid dependencies"):
        await orchestrator.plan("goal", identity)


async def test_plan_rejects_oversized_plan(identity) -> None:
    many = [
        {"id": f"s{i}", "description": "d", "task_type": "research", "depends_on": []}
        for i in range(8)
    ]
    orchestrator = Orchestrator(
        ScriptedModel(json.dumps({"subtasks": many})),
        FakeAuditSink(),
        lambda aid, role: None,
    )
    with pytest.raises(ModelFailure, match="out of bounds"):
        await orchestrator.plan("goal", identity)


async def test_plan_rejects_invalid_task_type(identity) -> None:
    bad = json.dumps(
        {"subtasks": [{"id": "s1", "description": "d", "task_type": "pigeon", "depends_on": []}]}
    )
    orchestrator = Orchestrator(ScriptedModel(bad), FakeAuditSink(), lambda aid, role: None)
    with pytest.raises(ModelFailure, match="invalid task_type"):
        await orchestrator.plan("goal", identity)
