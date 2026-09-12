"""STEP 4: the production loop must run through the lifecycle — and never
claim a state it could not persist. Real gateway/gate/filesystem; sinks are
recorded (no DB needed)."""
import asyncio
import json
from pathlib import Path

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.lifecycle import StateTransitionError, TaskState, TaskStatus
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

IDENT = SubjectIdentity(user_id="owner", session_id="s", task_id=None)

_POLICY_TMPL = (
    'version: "1.0.0"\n'
    "owner:\n"
    "  autonomy_level: autonomous\n"
    "  tools:\n"
    "    - id: fs-rule\n"
    "      tool: filesystem\n"
    "      risk: medium\n"
    "      default_decision: {decision}\n"
    "  external_network:\n"
    "    allow_public_get: true\n"
)


def _policy(tmp_path: Path, fs_decision: str) -> PolicyEngine:
    f = tmp_path / "owner.yaml"
    f.write_text(_POLICY_TMPL.format(decision=fs_decision), encoding="utf-8")
    p = PolicyEngine()
    p.load_file(f)
    return p


def build(tmp_path: Path, responses, fs_decision="allow"):
    gate = SecurityGate(_policy(tmp_path, fs_decision), PermissionEngine(), FakeAuditSink())
    gw = ToolGateway(gate, FakeAuditSink())
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gw.register(FilesystemTool(workspace_root=str(ws)))
    m = FakeLocalModel("fake/planner")
    m._responses = list(responses)
    mgr = ModelManager()
    mgr.register(m, status=ModelStatus.ENABLED)
    rt = AgentRuntime(
        selector=ModelSelector(
            mgr, ModelPerformanceTracker(), hardware=HardwareConstraints(ram_mb=8192)
        ),
        manager=mgr,
        gateway=gw,
        audit_sink=FakeAuditSink(),
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(),
        budget=TaskBudget(max_steps=4, max_retries=1),
        workspace_root=str(ws),
    )
    return rt, ws


def _rec_sink():
    calls: list[tuple[TaskStatus, TaskStatus]] = []

    async def sink(tid: str, expected: TaskStatus, to: TaskStatus) -> None:
        calls.append((expected, to))

    return sink, calls


WRITE = json.dumps(
    {
        "tool": "filesystem",
        "arguments": {"action": "write", "path": "l.txt", "content": "ok"},
        "finished": False,
    }
)
DONE = '{"finished": true}'


def test_success_walks_lifecycle_through_sink(tmp_path):
    sink, calls = _rec_sink()
    rt, ws = build(tmp_path, [WRITE, DONE])
    rep = asyncio.run(
        rt.execute_task("Write l.txt please", IDENT, max_steps=4, state_sink=sink)
    )
    assert rep.status is TaskStatus.SUCCESS
    assert (ws / "l.txt").read_text(encoding="utf-8").strip() == "ok"
    assert calls[0] == (TaskStatus.CREATED, TaskStatus.PLANNED)
    assert calls[-1] == (TaskStatus.RUNNING, TaskStatus.SUCCESS)
    assert [to for _, to in calls] == [
        TaskStatus.PLANNED,
        TaskStatus.AUTHORIZED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
    ]


def test_persist_failure_never_claims_success(tmp_path):
    async def dead_sink(tid, expected, to):  # simulates wedged DB
        raise ConnectionError("db down")

    rt, _ = build(tmp_path, [WRITE, DONE])
    rep = asyncio.run(
        rt.execute_task("Write l.txt please", IDENT, max_steps=4, state_sink=dead_sink)
    )
    assert rep.status is not TaskStatus.SUCCESS  # the permanent regression rule
    assert any("state persistence failed" in f for f in rep.failed)


def test_ask_without_approver_becomes_blocked(tmp_path):
    sink, calls = _rec_sink()
    rt, _ = build(tmp_path, [WRITE], fs_decision="ask")
    rep = asyncio.run(
        rt.execute_task("Write l.txt please", IDENT, max_steps=3, state_sink=sink)
    )
    assert rep.status is TaskStatus.BLOCKED
    assert (TaskStatus.RUNNING, TaskStatus.BLOCKED) in calls
    assert calls[-1][1] is TaskStatus.BLOCKED


def test_stop_requested_cancels_persistingly(tmp_path):
    sink, calls = _rec_sink()
    rt, _ = build(tmp_path, [WRITE, DONE])
    rep = asyncio.run(
        rt.execute_task(
            "Write l.txt please",
            IDENT,
            max_steps=4,
            state_sink=sink,
            stop_requested=lambda: True,
        )
    )
    assert rep.status is TaskStatus.CANCELLED
    assert calls[-1][1] is TaskStatus.CANCELLED


def test_retry_goes_failed_recovering_running(tmp_path):
    sink, calls = _rec_sink()
    bad = "not json at all"
    rt, _ = build(tmp_path, [bad] * 10)
    rep = asyncio.run(
        rt.execute_task("Do the thing", IDENT, max_steps=2, state_sink=sink)
    )
    assert rep.status is not TaskStatus.SUCCESS
    tos = [t for _, t in calls]
    assert TaskStatus.FAILED in tos
    assert TaskStatus.RECOVERING in tos  # never FAILED -> SUCCESS
    assert tos[tos.index(TaskStatus.RECOVERING) + 1] is TaskStatus.RUNNING


def test_duplicate_completion_illegal():
    st = TaskState(task_id="t", status=TaskStatus.CREATED)
    for s in (
        TaskStatus.PLANNED,
        TaskStatus.AUTHORIZED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCESS,
    ):
        st.transition(s)
    try:
        st.transition(TaskStatus.SUCCESS)
    except StateTransitionError as exc:
        assert "illegal" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate completion must be illegal")


def test_terminal_states_reject_everything_else():
    st = TaskState(task_id="t", status=TaskStatus.SUCCESS)
    for to in TaskStatus:
        try:
            st.transition(to)
            raise AssertionError(f"SUCCESS leaked a transition to {to}")
        except StateTransitionError:
            pass
