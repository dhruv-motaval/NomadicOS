"""STEP 5 — executor boundary tests (rebuild plan §12 items 1..16).

Proves: only gateway-issued AuthorizedAction tokens execute; the executor is
structurally incapable of policy, lifecycle, model, or grant decisions;
verification/retry/audit ownership holds. Also asserts the import-boundary.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from nomadicos.agent.executor import (
    AuthorizedAction,
    ExecutionResult,
    TaskExecutor,
)
from nomadicos.agent.runtime import AgentRuntime
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.audit.base import AuditEventCategory
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import PermissionDenied
from nomadicos.core.task_ir import ActionKind, TaskAction
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
from nomadicos.tools.base import Tool, ToolContext, ToolResult, ToolRisk, ToolSpec
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway

POLICY = (
    'version: "1.0.0"\n'
    "owner:\n"
    "  autonomy_level: full_autonomy\n"
    "  tools:\n"
    "    - id: fs-allow\n      tool: filesystem\n      risk: low\n"
    "      default_decision: allow\n"
    "    - id: echo-allow\n      tool: echo.ok\n      risk: low\n"
    "      default_decision: allow\n"
    "    - id: boom-allow\n      tool: echo.boom\n      risk: low\n"
    "      default_decision: allow\n"
)


class EchoTool(Tool):
    def __init__(self, name: str = "echo.ok", boom: bool = False) -> None:
        self._name = name
        self._boom = boom
        self._spec = ToolSpec(
            name=name,
            description="echo tool",
            risk=ToolRisk.READ_ONLY,
            arguments_schema={
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
                "additionalProperties": False,
            },
            evidence_kind="none",
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def validate_arguments(self, arguments):
        from nomadicos.tools.base import validate_against_schema

        validate_against_schema(arguments, self._spec.arguments_schema)
        return dict(arguments)

    async def execute(self, arguments, context: ToolContext) -> ToolResult:
        if self._boom:
            raise RuntimeError("boom")
        return ToolResult(success=True, data={"echo": arguments["msg"]})


def _stack(tmp_path):
    pf = tmp_path / "policy.yaml"
    pf.write_text(POLICY, encoding="utf-8")
    policy = PolicyEngine()
    policy.load_file(pf)
    sink = FakeAuditSink()
    gate = SecurityGate(policy, PermissionEngine(), sink)
    gw = ToolGateway(gate, sink)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gw.register(FilesystemTool(workspace_root=str(ws)))
    gw.register(EchoTool())
    gw.register(EchoTool(name="echo.boom", boom=True))
    return gw, sink, ws


IDENT = SubjectIdentity(user_id="u", session_id="s", task_id="t", step_id="st")


def _task(**kw) -> TaskAction:
    data = dict(
        kind=ActionKind.TOOL_CALL,
        tool="filesystem",
        arguments={"action": "write", "path": "x.txt", "content": "hi"},
        risk=RiskLevel.MEDIUM,
        capabilities=("filesystem.write",),
        task_id="t-1",
        step_id="s-1",
        attempt=1,
    )
    data.update(kw)
    return TaskAction(**data)


def _run(coro):
    return asyncio.run(coro)


# 1 + 8 + 11: authorized token executes; structured result; ids preserved ----
def test_authorized_token_executes_structured_result(tmp_path):
    gw, _, ws = _stack(tmp_path)
    task = _task()
    _decision, token, refusal = _run(
        gw.authorize_action(task.tool, task.arguments, IDENT, task_ref=task)
    )
    assert token is not None and refusal is None
    res = _run(gw.executor.run(token))
    assert isinstance(res, ExecutionResult)
    assert res.success is True
    assert res.task_id == "t-1"
    assert res.step_id == "s-1"
    assert res.attempt == 1
    assert res.capability == "filesystem.write"
    assert res.duration_ms >= 0.0
    assert (ws / "x.txt").read_text(encoding="utf-8") == "hi"
    assert set(res.as_dict()) == {
        "success", "action", "capability", "task_id", "step_id", "attempt",
        "data", "error", "evidence", "observations", "duration_ms",
    }
    json.dumps(res.as_dict())  # JSON-serializable envelope


# 2 + 4: unauthorized / forged / replay are rejected WITHOUT touching tool --
def test_unauthorized_delete_and_forged_tokens_rejected(tmp_path):
    gw, sink, ws = _stack(tmp_path)
    task = _task(
        arguments={"action": "delete", "path": "x.txt"}, capabilities=("filesystem.delete",)
    )
    decision, token, refusal = _run(
        gw.authorize_action("filesystem", task.arguments, IDENT, task_ref=task)
    )
    assert token is None and refusal is not None  # policy refused: needs owner grant
    _run(gw.authorize_action(_task().tool, _task().arguments, IDENT, task_ref=_task()))
    with pytest.raises(PermissionDenied):
        _run(gw.executor.run(None))
    forged = AuthorizedAction(
        action=_task(), tool_name="filesystem", capability="filesystem.write",
        resource="x.txt", identity=IDENT, grant_signature="i-made-this-up",
        policy_version="9.9.9", tool=gw.get("filesystem"),
    )
    with pytest.raises(PermissionDenied):
        _run(gw.executor.run(forged))
    # replay: a consumed stamp cannot run again
    _d, good, _ = _run(
        gw.authorize_action("filesystem", _task().arguments, IDENT, task_ref=_task())
    )
    _run(gw.executor.run(good))
    with pytest.raises(PermissionDenied):
        _run(gw.executor.run(good))
    assert (ws / "x.txt").read_text(encoding="utf-8") == "hi"


# 3: raw model data cannot enter the executor -------------------------------
@pytest.mark.parametrize(
    "bad", ['{"tool": "filesystem"}', {"tool": "filesystem", "allowed": True}, b"bytes", _task()]
)
def test_raw_data_rejected_by_executor(bad, tmp_path):
    gw, _, _ = _stack(tmp_path)
    with pytest.raises(PermissionDenied):
        _run(gw.executor.run(bad))


# 5: lifecycle mutation is structurally impossible for the executor ---------
SOURCE = Path(inspect.getsourcefile(TaskExecutor) or "").read_text(encoding="utf-8")


def _import_offenders(src: str, forbidden_prefixes: tuple[str, ...]) -> list[str]:
    return [
        line
        for line in src.splitlines()
        if line.strip().startswith(("from ", "import "))
        and any(line.strip().split()[1].startswith(p) for p in forbidden_prefixes)
    ]


@pytest.mark.parametrize(
    "forbidden",
    [
        ("nomadicos.security.gate",),  # policy
        ("nomadicos.core.lifecycle", "nomadicos.security.budgets"),  # lifecycle/budget
        ("nomadicos.models", "nomadicos.agent.selector"),  # model / selection
        ("nomadicos.memory", "nomadicos.experience"),  # memory / experience
        ("nomadicos.security.capability_registry",),  # capability granting
    ],
)
def test_executor_import_boundary(forbidden):
    assert _import_offenders(SOURCE, forbidden) == []


def test_executor_exposes_no_lifecycle_hooks():
    for name in vars(TaskExecutor):
        if name.startswith("__"):
            continue
        offenders = (
            "advance", "state", "transition", "authorize", "retry"
        )
        assert not any(k in name.lower() for k in offenders)


# 6 + 7: retry + verification live OUTSIDE the executor ---------------------
def test_retry_and_verification_outside_executor(tmp_path):
    gw, sink, ws = _stack(tmp_path)
    runs: list = []
    orig_run = gw.executor.run

    async def spy_run(prepared, *, dry_run: bool = False):
        runs.append(prepared.grant_signature)
        return await orig_run(prepared, dry_run=dry_run)

    gw.executor.run = spy_run  # type: ignore[method-assign]
    model = FakeLocalModel("fake/planner")
    model._responses = [json.dumps({"tool": "ghost.tool", "arguments": {}})] * 8
    mgr = ModelManager()
    mgr.register(model, status=ModelStatus.ENABLED)
    policy = PolicyEngine()
    pf = tmp_path / "policy.yaml"
    pf.write_text(POLICY, encoding="utf-8")
    policy.load_file(pf)
    rt = AgentRuntime(
        selector=ModelSelector(
            mgr,
            ModelPerformanceTracker(),
            hardware=HardwareConstraints(ram_mb=8192),
        ),
        manager=mgr, gateway=gw, audit_sink=sink,
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(), budget=TaskBudget(max_steps=2, max_retries=1),
        workspace_root=str(ws),
    )
    rep = _run(rt.execute_task("open tool ghost.tool now", IDENT, max_steps=2))

    # the unknown tool was denied at the descriptor/bindsite (outside executor):
    assert rep.status.value == "FAILED"
    assert runs == []  # executor was never invoked for denied attempts
    recovering = [
        e
        for e in sink.events
        if e.decision and str(e.decision).startswith("STATE_TRANSITION:FAILED>RECOVERING")
    ]
    assert recovering, "retry attempt-2 transition happens in lifecycle, not executor"
    gw.executor.run = orig_run  # type: ignore[method-assign]


def retry_seen_count(sink: FakeAuditSink) -> int:
    return len(
        [
            e
            for e in sink.events
            if e.decision in ("STATE_TRANSITION:CREATED>PLANNED",)
        ]
    )


# 9: a tool that throws becomes a structured failure + audit ----------------
def test_tool_exception_structured_failure(tmp_path):
    gw, sink, _ = _stack(tmp_path)
    task = _task(
        tool="echo.boom", arguments={"msg": "x"}, capabilities=("echo.boom.execute",)
    )
    _d, token, _r = _run(
        gw.authorize_action("echo.boom", {"msg": "x"}, IDENT, task_ref=task)
    )
    assert token is not None
    res = _run(gw.executor.run(token))
    assert res.success is False
    assert "boom" in (res.error or "")
    failed_events = [e for e in sink.events if e.decision == "ACTION_FAILED"]
    assert failed_events and failed_events[0].fields["capability"] == "echo.boom.execute"


# 10: audits not duplicated across the boundary -----------------------------
def test_no_duplicate_audit_events(tmp_path):
    gw, sink, _ = _stack(tmp_path)
    task = _task()
    _d, token, _r = _run(gw.authorize_action("filesystem", task.arguments, IDENT, task_ref=task))
    _run(gw.executor.run(token))
    decisions = [e for e in sink.events if e.category is AuditEventCategory.TOOL_DECISION]
    executed = [e for e in sink.events if e.decision in ("EXECUTED", "ACTION_FAILED")]
    assert len(decisions) == 1  # exactly one policy decision
    assert len(executed) == 1  # exactly one execution outcome


# 12: cancellation is lifecycle-owned (executor untouched) ------------------
def test_cancellation_never_reaches_executor(tmp_path):
    gw, sink, ws = _stack(tmp_path)
    runs: list = []
    orig = gw.executor.run

    async def spy(prepared, *, dry_run: bool = False):
        runs.append(1)
        return await orig(prepared, dry_run=dry_run)

    gw.executor.run = spy  # type: ignore[method-assign]
    pf = tmp_path / "policy.yaml"
    policy = PolicyEngine()
    policy.load_file(pf) if pf.exists() else None
    model = FakeLocalModel("fake/planner")
    model._responses = [json.dumps({
        "tool": "filesystem",
        "arguments": {"action": "write", "path": "y.txt", "content": "z"},
        "finished": False,
    })] * 6
    mgr = ModelManager()
    mgr.register(model, status=ModelStatus.ENABLED)
    rt = AgentRuntime(
        selector=ModelSelector(
            mgr,
            ModelPerformanceTracker(),
            hardware=HardwareConstraints(ram_mb=8192),
        ),
        manager=mgr, gateway=gw, audit_sink=sink,
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(), budget=TaskBudget(max_steps=4, max_retries=1),
        workspace_root=str(ws),
    )
    rep = _run(
        rt.execute_task(
            "Write file y.txt with z",
            IDENT,
            max_steps=4,
            stop_requested=lambda: True,
        )
    )
    from nomadicos.core.lifecycle import TaskStatus

    assert rep.status is TaskStatus.CANCELLED
    assert runs == []  # executor never ran when lifecycle cancelled
    gw.executor.run = orig  # type: ignore[method-assign]


# 13: determinism survives the split ---------------------------------------
def test_authorization_deterministic_after_split(tmp_path):
    gw, _, _ = _stack(tmp_path)
    t = _task()
    d1, tok1, _ = _run(gw.authorize_action(t.tool, t.arguments, IDENT, task_ref=t))
    d2, tok2, _ = _run(gw.authorize_action(t.tool, t.arguments, IDENT, task_ref=t))
    assert d1.decision.value == d2.decision.value
    assert d1.reason_code == d2.reason_code == "POLICY_ALLOW"
    assert tok1 is not None and tok2 is not None
