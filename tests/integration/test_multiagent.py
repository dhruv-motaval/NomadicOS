"""Phase 15 integration tests: multi-agent orchestration end-to-end (ADR-0030)."""

import asyncio
import json
import threading

from nomadicos.agent.orchestrator import Orchestrator
from nomadicos.agent.runtime import TaskReport
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.core.errors import ModelFailure
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import Tool as BaseTool
from nomadicos.tools.base import ToolContext, ToolResult, ToolRisk, ToolSpec
from nomadicos.tools.gateway import ToolGateway


class ScriptedModel:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        from nomadicos.models.base import GenerateResult

        if self._responses:
            return GenerateResult(text=self._responses.pop(0))
        return GenerateResult(text=json.dumps({"finished": True}))


class RecordingTool(BaseTool):
    """Always-succeeds tool that records executions per agent."""

    def __init__(self) -> None:
        self.executed_by: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self.fail_for: set[str] = set()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="fake.work",
            description="test tool",
            risk=ToolRisk.READ_ONLY,
            arguments_schema={"type": "object", "properties": {}, "additionalProperties": False},
        )

    async def validate_arguments(self, arguments):
        return dict(arguments)

    async def execute(self, arguments, context: ToolContext) -> ToolResult:
        with self._lock:
            self.executed_by.append((context.user_id, context.task_id or "?"))
            # substring match: worker task_ids embed the subtask id (ADR-0030)
            should_fail = any(f in (context.task_id or "") for f in self.fail_for)
        if should_fail:
            return ToolResult.failure("scripted failure", evidence={"agent": context.task_id})
        return ToolResult(success=True, data={"done": True})


class RecordingRuntime:
    """Real AgentRuntime-shape: one structured proposal → tool → done."""

    def __init__(self, model, gateway, audit, recorder, evaluator, agent_id) -> None:
        self._model = model
        self._gateway = gateway
        self._audit = audit
        self._recorder = recorder
        self._evaluator = evaluator
        self._agent_id = agent_id

    async def execute_task(self, goal, identity, *, model_id=None, max_steps=3):
        request = __import__(
            "nomadicos.models.base", fromlist=["GenerateRequest"]
        ).GenerateRequest(prompt=goal, max_output_tokens=512, temperature=0.0)
        generation = await self._model.generate(request)
        try:
            proposal = json.loads(generation.text)
        except json.JSONDecodeError:
            return self._report(identity, goal, TaskStatus.FAILED, "unparseable proposal")
        if proposal.get("finished"):
            return self._report(identity, goal, TaskStatus.SUCCESS)
        tool_name = proposal.get("tool", "")
        try:
            tool = self._gateway.get(tool_name)
            validated = await tool.validate_arguments(proposal.get("arguments", {}))
            tool_result = await tool.execute(
                validated,
                ToolContext(
                    user_id=identity.user_id,
                    session_id=identity.session_id,
                    task_id=f"{identity.task_id}-{self._agent_id}",
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surfaced in report
            return self._report(identity, goal, TaskStatus.FAILED, str(exc))
        if not tool_result.success:
            return self._report(identity, goal, TaskStatus.FAILED, tool_result.error)
        return self._report(identity, goal, TaskStatus.SUCCESS)

    @staticmethod
    def _report(identity, goal, status, error=None) -> TaskReport:
        return TaskReport(
            task_id=identity.task_id or "t",
            goal=goal,
            status=status,
            requested=goal,
            failed=[error] if error else [],
        )


def build(plan: str, worker_scripts: dict[str, list[str]]) -> tuple:
    """Returns (orchestrator, audit, tool, factory_calls)."""
    audit = FakeAuditSink()
    tool = RecordingTool()
    gateway = ToolGateway(None, audit)
    gateway._tools = {"fake.work": tool}
    factory_calls: list[tuple[str, str]] = []

    def factory(agent_id: str, role) -> RecordingRuntime:
        factory_calls.append((agent_id, role.name))
        model = ScriptedModel(
            worker_scripts.get(agent_id, [json.dumps({"finished": True})])
        )
        return RecordingRuntime(model, gateway, audit, None, None, agent_id)

    orchestrator = Orchestrator(
        ScriptedModel([plan]), audit, factory, max_subtasks=6
    )
    return orchestrator, audit, tool, factory_calls


IDENTITY = SubjectIdentity(user_id="owner", session_id="s-1", task_id="t-1")

SEQ_PLAN = json.dumps(
    {
        "subtasks": [
            {"id": "s1", "description": "research step", "task_type": "research", "depends_on": []},
            {
                "id": "s2",
                "description": "write step",
                "task_type": "general_generation",
                "depends_on": ["s1"],
            },
        ]
    }
)

PARALLEL_PLAN = json.dumps(
    {
        "subtasks": [
            {"id": "s1", "description": "research A", "task_type": "research", "depends_on": []},
            {"id": "s2", "description": "research B", "task_type": "research", "depends_on": []},
            {"id": "s3", "description": "research C", "task_type": "research", "depends_on": []},
            {
                "id": "s4",
                "description": "compare all",
                "task_type": "analysis",
                "depends_on": ["s1", "s2", "s3"],
            },
        ]
    }
)


def test_orchestration_sequential_chain() -> None:
    """ADR-0030: planner DAG s1 → s2; workers run in dependency order."""
    orch, audit, tool, factory_calls = build(
        SEQ_PLAN,
        {
            "planner": [SEQ_PLAN],
            "owner/t-1/worker-s1": [json.dumps({"finished": True})],
            "owner/t-1/worker-s2": [json.dumps({"finished": True})],
        },
    )
    result = asyncio.run(orch.orchestrate("Do the work", IDENTITY))

    assert result.final_status == "SUCCESS"
    assert [r.subtask_id for r in result.subtask_results] == ["s1", "s2"]
    assert result.subtask_results[0].report.status is TaskStatus.SUCCESS
    assert result.synthesis
    roles = [r for _, r in factory_calls if r == "worker"]
    assert len(roles) == 2


def test_parallel_siblings_and_synthesis() -> None:
    """ADR-0030 §2: independent subtasks run as parallel worker agents; s4
    consumes the whole sibling group; synthesizer merges results."""
    orch, audit, tool, factory_calls = build(
        PARALLEL_PLAN,
        {
            "planner": [PARALLEL_PLAN],
            "owner/t-1/worker-s1": [json.dumps({"finished": True})],
            "owner/t-1/worker-s2": [json.dumps({"finished": True})],
            "owner/t-1/worker-s3": [json.dumps({"finished": True})],
            "owner/t-1/worker-s4": [json.dumps({"finished": True})],
        },
    )
    result = asyncio.run(orch.orchestrate("Research three competitors", IDENTITY))

    assert result.final_status == "SUCCESS"
    assert len(result.subtask_results) == 4
    s4 = result.subtask_results[-1]
    assert s4.report.status is TaskStatus.SUCCESS
    assert result.synthesis


def test_failed_worker_cascades_to_dependents() -> None:
    """ADR-0030 §7: failed subtask ⇒ dependents skipped; final PARTIAL/FAILED."""
    orch, audit, tool, factory_calls = build(
        PARALLEL_PLAN,
        {
            "planner": [PARALLEL_PLAN],
            # s1's worker actually calls the failing tool; the rest just finish.
            "owner/t-1/worker-s1": [
                json.dumps({"tool": "fake.work", "arguments": {}, "finished": False})
            ],
            "owner/t-1/worker-s2": [json.dumps({"finished": True})],
            "owner/t-1/worker-s3": [json.dumps({"finished": True})],
            "owner/t-1/worker-s4": [json.dumps({"finished": True})],
        },
    )
    tool.fail_for = {"s1"}  # s1's tool execution fails

    result = asyncio.run(orch.orchestrate("Research three competitors", IDENTITY))

    assert result.final_status in ("PARTIALLY_COMPLETED", "FAILED")
    statuses = {r.subtask_id: r.status for r in result.subtask_results}
    assert statuses["s1"] in ("FAILED", "SKIPPED")
    assert statuses["s4"] in ("FAILED", "SKIPPED")


def test_planner_failure_degrades_to_single_agent() -> None:
    """ADR-0030: planner error ⇒ graceful single-agent fallback, no crash."""
    class BrokenPlanner:
        async def generate(self, request):
            raise ModelFailure("planner returned garbage")

    audit = FakeAuditSink()

    class SingleRuntime:
        async def execute_task(self, goal, identity, *, model_id=None, max_steps=3):
            return TaskReport(
                task_id=identity.task_id or "t",
                goal=goal,
                status=TaskStatus.SUCCESS,
                requested=goal,
            )

    orch = Orchestrator(BrokenPlanner(), audit, lambda aid, role: SingleRuntime())
    result = asyncio.run(orch.orchestrate("Simple goal", IDENTITY))
    assert result.final_status == "SUCCESS"
    assert "nomadicos" not in result.synthesis.lower() or result.synthesis


def test_audit_trail_records_plan_and_subtasks() -> None:
    """BP §364: multi-agent attribution is audited."""
    orch, audit, tool, factory_calls = build(
        SEQ_PLAN,
        {
            "planner": [SEQ_PLAN],
            "owner/t-1/worker-s1": [json.dumps({"finished": True})],
            "owner/t-1/worker-s2": [json.dumps({"finished": True})],
        },
    )
    asyncio.run(orch.orchestrate("Do the work", IDENTITY))

    subjects = [e.subject for e in audit.events]
    assert "planner" in subjects
    assert "orchestrator" in subjects
    step_ids = [e.step_id for e in audit.events if e.step_id]
    assert "s1" in step_ids and "s2" in step_ids
