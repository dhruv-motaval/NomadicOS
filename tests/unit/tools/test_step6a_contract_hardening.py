"""STEP 6A — Tool Result Contract Hardening & Lifecycle Separation Regression Suite.

Tests 1 to 16 cover all required invariants:
1. Valid structured tool result
2. Tool failure result
3. Timeout result
4. Malformed result
5. Execution success != verification success
6. Raw model output cannot reach executor
7. Tool cannot mutate lifecycle
8. Tool cannot grant capability
9. Cancellation cleanup
10. Task_id preserved
11. Step_id preserved
12. Attempt preserved
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from nomadicos.agent.executor import AuthorizedAction, ExecutionResult, TaskExecutor
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import PermissionDenied, ToolExecutionError
from nomadicos.core.lifecycle import TaskState, TaskStatus
from nomadicos.core.task_ir import ActionClaim, ActionKind, TaskAction
from nomadicos.evaluation.base import Evidence
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.security.capability_registry import resolve
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity
from nomadicos.tools.base import ToolContext
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway
from nomadicos.tools.terminal import TerminalTool

POLICY = """version: "1.0.0"
owner:
  autonomy_level: full_autonomy
  tools:
    - id: fs-allow
      tool: filesystem
      risk: low
      default_decision: allow
    - id: term-allow
      tool: terminal
      risk: high
      default_decision: allow
"""


def _setup_stack(tmp_path: Path):
    pf = tmp_path / "policy.yaml"
    pf.write_text(POLICY, encoding="utf-8")
    policy = PolicyEngine()
    policy.load_file(pf)
    perms = PermissionEngine()
    audit = FakeAuditSink()
    gate = SecurityGate(policy, perms, audit)
    gw = ToolGateway(gate, audit)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gw.register(FilesystemTool(workspace_root=str(ws)))
    gw.register(TerminalTool(workspace_root=str(ws)))
    return gw, ws, audit


def _make_action(
    tool: str,
    arguments: dict[str, Any],
    task_id: str = "task-t1",
    step_id: str = "step-s1",
    attempt: int = 1,
) -> TaskAction:
    claim = ActionClaim(kind=ActionKind.TOOL_CALL, tool=tool, arguments=arguments, finish=False)
    cap = resolve(tool, arguments)
    return TaskAction.bind(
        claim,
        task_id=task_id,
        step_id=step_id,
        attempt=attempt,
        risk=cap.risk,
        capabilities=(cap.id,),
    )


# ---------------------------------------------------------------------------
# 1. Valid structured tool result
# ---------------------------------------------------------------------------
def test_regression_1_valid_structured_tool_result(tmp_path: Path) -> None:
    gw, ws, _ = _setup_stack(tmp_path)
    ident = SubjectIdentity(user_id="u", session_id="s", task_id="t-1", step_id="s-1")
    action = _make_action(
        "filesystem",
        {"action": "write", "path": "output.txt", "content": "hello world"},
        task_id="t-1",
        step_id="s-1",
        attempt=1,
    )

    async def go():
        _decision, token, _refusal = await gw.authorize_action(
            "filesystem", action.arguments, ident, task_ref=action
        )
        assert token is not None
        return await gw.executor.run(token)

    res: ExecutionResult = asyncio.run(go())
    assert res.success is True
    assert res.action == "filesystem"
    assert res.capability == "filesystem.write"
    assert res.task_id == "t-1"
    assert res.step_id == "s-1"
    assert res.attempt == 1
    assert res.error is None
    assert res.error_code is None
    assert isinstance(res.data, dict)
    assert res.data.get("bytes_written") == len("hello world")
    assert res.evidence.get("exists") is True
    assert (ws / "output.txt").read_text(encoding="utf-8") == "hello world"


# ---------------------------------------------------------------------------
# 2. Tool failure result
# ---------------------------------------------------------------------------
def test_regression_2_tool_failure_result(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    ident = SubjectIdentity(user_id="u", session_id="s", task_id="t-2", step_id="s-2")
    action = _make_action(
        "filesystem",
        {"action": "read", "path": "nonexistent_file.txt"},
        task_id="t-2",
        step_id="s-2",
        attempt=2,
    )

    async def go():
        _decision, token, _refusal = await gw.authorize_action(
            "filesystem", action.arguments, ident, task_ref=action
        )
        assert token is not None
        return await gw.executor.run(token)

    res: ExecutionResult = asyncio.run(go())
    assert res.success is False
    assert res.task_id == "t-2"
    assert res.step_id == "s-2"
    assert res.attempt == 2
    assert res.error == "file does not exist"
    assert res.error_code == "FILE_NOT_FOUND"
    assert res.evidence.get("exists") is False


# ---------------------------------------------------------------------------
# 3. Timeout result (no orphan process)
# ---------------------------------------------------------------------------
def _count_procs(name: str) -> int:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.lower().count(name.lower())
    except Exception:
        return 0


def test_regression_3_timeout_result(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    ident = SubjectIdentity(user_id="u", session_id="s", task_id="t-3", step_id="s-3")
    action = _make_action(
        "terminal",
        {
            "command": "python",
            "args": ["-c", "import time; time.sleep(30)"],
            "timeout_seconds": 0.5,
        },
        task_id="t-3",
        step_id="s-3",
        attempt=1,
    )
    before_py = _count_procs("python.exe")

    async def go():
        _decision, token, _refusal = await gw.authorize_action(
            "terminal", action.arguments, ident, task_ref=action
        )
        assert token is not None
        return await gw.executor.run(token)

    res = asyncio.run(go())
    assert res.success is False
    assert res.error_code == "COMMAND_TIMEOUT"
    assert "timed out" in (res.error or "")
    time.sleep(0.5)
    after_py = _count_procs("python.exe")
    assert after_py <= before_py


# ---------------------------------------------------------------------------
# 4. Malformed result (rejected at schema before executor)
# ---------------------------------------------------------------------------
def test_regression_4_malformed_result(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    ident = SubjectIdentity(user_id="u", session_id="s", task_id="t-4", step_id="s-4")

    async def go():
        # Missing required 'action' in filesystem arguments
        await gw.authorize_action("filesystem", {"path": "test.txt"}, ident)

    with pytest.raises(ToolExecutionError) as exc_info:
        asyncio.run(go())
    assert exc_info.value.context.get("code") == "INVALID_TOOL_ARGS"


# ---------------------------------------------------------------------------
# 5. Execution success != verification success
# ---------------------------------------------------------------------------
def test_regression_5_execution_success_is_not_verification_success(tmp_path: Path) -> None:
    """A tool can exit 0 / return success=True, but domain verification fails."""
    gw, ws, _ = _setup_stack(tmp_path)
    evaluator = EvaluationEngine()

    # Terminal command that exits 0 cleanly but produces EMPTY output
    # (e.g. python -c "pass")
    action = _make_action(
        "terminal",
        {"command": "python", "args": ["-c", "pass"]},
        task_id="t-5",
    )
    ident = SubjectIdentity(user_id="u", session_id="s", task_id="t-5", step_id="s-5")

    async def go():
        _decision, token, _ = await gw.authorize_action(
            "terminal", action.arguments, ident, task_ref=action
        )
        assert token is not None
        exec_res = await gw.executor.run(token)
        assert exec_res.success is True  # Tool execution itself succeeded (exit 0)

        # Domain verification: TerminalVerifier requires both exit_code==0 AND non-empty output
        evidence = Evidence(
            kind="terminal",
            facts={
                "exit_code": exec_res.data.get("exit_code"),
                "stdout": exec_res.data.get("stdout", ""),
            },
        )
        verdict = await evaluator.verify("terminal", evidence)
        return exec_res, verdict

    exec_res, verdict = asyncio.run(go())
    assert exec_res.success is True  # EXECUTED successfully
    # But verification fails because stdout was empty!
    assert verdict.verified is False
    assert any(c.name == "output_not_empty" and not c.passed for c in verdict.checks)


# ---------------------------------------------------------------------------
# 6. Raw model output cannot reach executor
# ---------------------------------------------------------------------------
def test_regression_6_raw_model_output_cannot_reach_executor(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    executor: TaskExecutor = gw.executor

    # 1. Raw dict
    with pytest.raises(PermissionDenied, match="executor boundary violated"):
        asyncio.run(
            executor.run({"tool": "filesystem", "arguments": {"action": "write"}})  # type: ignore[arg-type]
        )

    # 2. Raw JSON string
    with pytest.raises(PermissionDenied, match="executor boundary violated"):
        asyncio.run(executor.run('{"tool": "filesystem"}'))  # type: ignore[arg-type]

    # 3. Unauthenticated TaskAction
    action = _make_action("filesystem", {"action": "list", "path": "."})
    with pytest.raises(PermissionDenied, match="executor boundary violated"):
        asyncio.run(executor.run(action))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. Tool cannot mutate lifecycle
# ---------------------------------------------------------------------------
def test_regression_7_tool_cannot_mutate_lifecycle(tmp_path: Path) -> None:
    """Assert tool interfaces have no access to task state or transition machinery."""
    gw, ws, _ = _setup_stack(tmp_path)
    fs_tool = gw.get("filesystem")

    # Neither tool nor ToolContext accepts or exposes TaskState or transitions
    context = ToolContext(user_id="u", task_id="t-7")
    assert not hasattr(context, "status")
    assert not hasattr(context, "transition")
    assert not hasattr(context, "advance")

    # Tool execution signature does not permit passing state
    execute_sig = inspect.signature(fs_tool.execute)
    assert set(execute_sig.parameters.keys()) == {"arguments", "context"}

    state = TaskState(task_id="t-7", status=TaskStatus.CREATED)
    assert state.status == TaskStatus.CREATED
    # Tool execution cannot mutate state
    res = asyncio.run(
        fs_tool.execute({"action": "write", "path": "safe.txt", "content": "ok"}, context)
    )
    assert res.success is True
    assert state.status == TaskStatus.CREATED  # Untouched


# ---------------------------------------------------------------------------
# 8. Tool cannot grant capability
# ---------------------------------------------------------------------------
def test_regression_8_tool_cannot_grant_capability(tmp_path: Path) -> None:
    """Forged token or replay of consumed grant stamp is rejected by executor."""
    gw, _, _ = _setup_stack(tmp_path)
    action = _make_action("filesystem", {"action": "read", "path": "safe.txt"})
    ident = SubjectIdentity(user_id="u", task_id="t-8")

    # Attempt to manufacture an AuthorizedAction without gateway registration
    forged_token = AuthorizedAction(
        action=action,
        tool_name="filesystem",
        capability="filesystem.read",
        resource="safe.txt",
        identity=ident,
        grant_signature="forged-grant-signature-xyz",
        policy_version="1.0.0",
        tool=gw.get("filesystem"),
    )

    with pytest.raises(PermissionDenied, match="executor boundary violated"):
        asyncio.run(gw.executor.run(forged_token))


# ---------------------------------------------------------------------------
# 9. Cancellation cleanup
# ---------------------------------------------------------------------------
def test_regression_9_cancellation_cleanup(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    ident = SubjectIdentity(user_id="u", task_id="t-9")
    action = _make_action(
        "terminal",
        {
            "command": "python",
            "args": ["-c", "import time; time.sleep(30)"],
            "timeout_seconds": 20.0,
        },
        task_id="t-9",
    )
    before_py = _count_procs("python.exe")

    async def run_and_cancel():
        _decision, token, _ = await gw.authorize_action(
            "terminal", action.arguments, ident, task_ref=action
        )
        assert token is not None
        task = asyncio.create_task(gw.executor.run(token))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.2)

    asyncio.run(run_and_cancel())
    time.sleep(0.5)
    after_py = _count_procs("python.exe")
    assert after_py <= before_py


# ---------------------------------------------------------------------------
# 10. Task_id preserved
# ---------------------------------------------------------------------------
def test_regression_10_task_id_preserved(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    test_id = "task-preserve-uuid-999"
    ident = SubjectIdentity(user_id="u", task_id=test_id, step_id="s-10")
    action = _make_action(
        "filesystem",
        {"action": "write", "path": "p.txt", "content": "10"},
        task_id=test_id,
        step_id="s-10",
    )

    async def go():
        _decision, token, _ = await gw.authorize_action(
            "filesystem", action.arguments, ident, task_ref=action
        )
        assert token is not None
        assert token.action.task_id == test_id
        return await gw.executor.run(token)

    res = asyncio.run(go())
    assert res.task_id == test_id


# ---------------------------------------------------------------------------
# 11. Step_id preserved
# ---------------------------------------------------------------------------
def test_regression_11_step_id_preserved(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    test_step = "attempt-3-step-14"
    ident = SubjectIdentity(user_id="u", task_id="t-11", step_id=test_step)
    action = _make_action(
        "filesystem",
        {"action": "write", "path": "p.txt", "content": "11"},
        task_id="t-11",
        step_id=test_step,
    )

    async def go():
        _decision, token, _ = await gw.authorize_action(
            "filesystem", action.arguments, ident, task_ref=action
        )
        assert token is not None
        assert token.action.step_id == test_step
        return await gw.executor.run(token)

    res = asyncio.run(go())
    assert res.step_id == test_step


# ---------------------------------------------------------------------------
# 12. Attempt preserved
# ---------------------------------------------------------------------------
def test_regression_12_attempt_preserved(tmp_path: Path) -> None:
    gw, _, _ = _setup_stack(tmp_path)
    test_attempt = 4
    ident = SubjectIdentity(user_id="u", task_id="t-12", step_id="s-12")
    action = _make_action(
        "filesystem",
        {"action": "write", "path": "p.txt", "content": "12"},
        task_id="t-12",
        step_id="s-12",
        attempt=test_attempt,
    )

    async def go():
        _decision, token, _ = await gw.authorize_action(
            "filesystem", action.arguments, ident, task_ref=action
        )
        assert token is not None
        assert token.action.attempt == test_attempt
        return await gw.executor.run(token)

    res = asyncio.run(go())
    assert res.attempt == test_attempt


# ---------------------------------------------------------------------------
# 13. Terminal working_dir shares filesystem path confinement (STEP 6A parity)
# ---------------------------------------------------------------------------
def test_regression_13_terminal_working_dir_confined(tmp_path: Path) -> None:
    tool = TerminalTool(workspace_root=str(tmp_path / "ws"))

    async def validated(working_dir: str) -> str:
        args = await tool.validate_arguments(
            {"command": "node", "args": ["t.js"], "working_dir": working_dir}
        )
        return args["working_dir"]

    resolved = asyncio.run(validated("proj"))
    assert Path(resolved) == (tmp_path / "ws" / "proj").resolve()
    with pytest.raises(Exception) as exc:
        asyncio.run(validated("../outside"))
    assert "PATH_ESCAPE" in str(getattr(exc.value, "context", "")) or "workspace" in str(exc.value)


# ---------------------------------------------------------------------------
# 14. Sensitive-data ASK is resolvable by owner one-shot confirmation (BP §36.2)
# ---------------------------------------------------------------------------
def test_regression_14_sensitive_ask_resolved_by_owner_grant(tmp_path: Path) -> None:
    gw, _, audit = _setup_stack(tmp_path)
    gate = gw._gate
    perms = gate._permissions
    ident = SubjectIdentity(user_id="u", task_id="t-14", step_id="s-14")

    async def decide():
        return await gate.authorize(
            tool="filesystem",
            risk=RiskLevel.LOW,
            identity=ident,
            classification="sensitive",
        )

    first = asyncio.run(decide())
    assert first.decision.value == "ASK"  # uncertain content, no approver yet
    perms.grant("filesystem", granted_by="owner", one_shot=True)
    second = asyncio.run(decide())
    assert second.decision.value == "ALLOW"  # the grant IS the confirmation
    third = asyncio.run(decide())
    assert third.decision.value == "ASK"  # one-shot consumed: confirmation does not persist
    assert audit is not None


# ---------------------------------------------------------------------------
# 15. Filesystem write/read is a byte-exact round-trip (no newline rewriting)
# ---------------------------------------------------------------------------
def test_regression_15_write_read_roundtrip(tmp_path: Path) -> None:
    tool = FilesystemTool(workspace_root=str(tmp_path))
    ctx = ToolContext(task_id="t", step_id="s", user_id="u")
    text = "line1\nline2\nends-with-newline\n"
    asyncio.run(tool.execute({"action": "write", "path": "rt.txt", "content": text}, ctx))
    back = asyncio.run(tool.execute({"action": "read", "path": "rt.txt"}, ctx))
    assert back.success and back.data["content"] == text


# ---------------------------------------------------------------------------
# 16. Token must carry the GATEWAY-VALIDATED arguments, not the raw task_ref
# ---------------------------------------------------------------------------
def test_regression_16_token_carries_validated_arguments(tmp_path: Path) -> None:
    gw, ws, _ = _setup_stack(tmp_path)
    args = {"command": "node", "args": ["x.js"], "working_dir": "proj"}
    ident = SubjectIdentity(user_id="u", task_id="t-16", step_id="s-16")
    action = _make_action("terminal", args, task_id="t-16", step_id="s-16")

    async def go():
        _d, token, _r = await gw.authorize_action("terminal", args, ident, task_ref=action)
        assert token is not None
        return token

    token = asyncio.run(go())
    confined = token.action.arguments["working_dir"]
    assert Path(confined).is_absolute() and Path(confined) == (ws / "proj")
