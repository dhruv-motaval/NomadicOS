"""STEP 6A — tool result-contract matrix across EVERY registered tool.

Failure modes must arrive as STRUCTURED results (error_code + identity
correlation) at both boundaries (authorize raise, policy refusal, executor
dispatch); success must carry evidence; timeouts must leave no orphan.
"""

from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

from nomadicos.agent.executor import ExecutionResult
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.task_ir import ActionClaim, ActionKind, TaskAction
from nomadicos.network.base import NetworkResponse
from nomadicos.network.fake import FakeNetworkTransport
from nomadicos.network.gateway import NetworkGateway
from nomadicos.network.web_tool import WebFetchTool
from nomadicos.security.capability_registry import resolve
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway
from nomadicos.tools.generated import GeneratedScriptTool, save_generated_script
from nomadicos.tools.terminal import TerminalTool

IDENT = SubjectIdentity(user_id="u", session_id="s", task_id="t-1", step_id="a1-s1")

_POLICY = """version: "1.0.0"
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
    - id: web-allow
      tool: web.fetch
      risk: medium
      default_decision: allow
    - id: gen-allow
      tool: script.flaky
      risk: high
      default_decision: allow
  external_network:
    allow_public_get: true
    denied_domains: ["blocked.test"]
"""


def _stack(tmp_path, extra_script: str | None = None):
    pf = tmp_path / "policy.yaml"
    pf.write_text(_POLICY, encoding="utf-8")
    policy = PolicyEngine()
    policy.load_file(pf)
    perms = PermissionEngine()
    audit = FakeAuditSink()
    gate = SecurityGate(policy, perms, audit)
    gw = ToolGateway(gate, audit)
    ws = tmp_path / "ws"
    ws.mkdir()
    gw.register(FilesystemTool(workspace_root=str(ws)))
    gw.register(TerminalTool(workspace_root=str(ws)))
    net = NetworkGateway(
        gate,
        audit,
        FakeNetworkTransport(
            {
                "https://ok.test": NetworkResponse(
                    status=200, body=b"<h1>Public page</h1>", url="x", elapsed_ms=1.0
                )
            }
        ),
    )
    gw.register(WebFetchTool(net))
    if extra_script is not None:
        script_path = save_generated_script(ws, "flaky", extra_script)
        assert script_path is not None
        gw.register(GeneratedScriptTool(script_path))
        perms.grant("script.flaky", granted_by="cli", one_shot=False)
    return gw, audit, ws


def _attempt(gw, tool, arguments):
    """Return (path, structured-result). path: exec|refused|raised."""

    async def go():
        claim = ActionClaim(kind=ActionKind.TOOL_CALL, tool=tool, arguments=arguments, finish=False)
        cap = resolve(tool, arguments)
        action = TaskAction.bind(
            claim,
            task_id=IDENT.task_id or "t-1",
            step_id=IDENT.step_id or "a1-s1",
            attempt=3,
            risk=cap.risk,
            capabilities=(cap.id,),
        )
        try:
            decision, token, refusal = await gw.authorize_action(
                tool, arguments, IDENT, task_ref=action
            )
        except Exception as exc:  # gateway-boundary raise: map to envelope
            code = str(getattr(exc, "context", {}).get("code") or "TOOL_EXCEPTION")
            return "raised", ExecutionResult(
                success=False,
                action=tool,
                capability=cap.id,
                task_id=action.task_id,
                step_id=action.step_id,
                attempt=action.attempt,
                error=str(exc),
                error_code=code,
            )
        if token is None:
            assert refusal is not None
            return "refused", refusal
        return "exec", await gw.executor.run(token)

    return asyncio.run(go())


# --------------------------------------------------------------- filesystem --
def test_filesystem_escape_is_structured_code(tmp_path):
    gw, _, _ = _stack(tmp_path)
    kind, res = _attempt(gw, "filesystem", {"action": "read", "path": "C:\\Windows\\x"})
    assert res.success is False
    assert res.error_code == "PATH_ESCAPE"


def test_filesystem_write_success_carries_identity(tmp_path):
    gw, _, ws = _stack(tmp_path)
    kind, res = _attempt(gw, "filesystem", {"action": "write", "path": "a.txt", "content": "x"})
    assert kind == "exec" and res.success
    assert (res.task_id, res.step_id, res.attempt) == ("t-1", "a1-s1", 3)
    assert res.error_code is None
    assert (ws / "a.txt").read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------- terminal --
def test_terminal_missing_command_is_structured(tmp_path):
    gw, _, _ = _stack(tmp_path)
    kind, res = _attempt(gw, "terminal", {"command": "definitely-not-a-command"})
    assert kind == "exec" and res.success is False
    assert res.error_code == "COMMAND_NOT_FOUND"
    assert (res.task_id, res.step_id, res.attempt) == ("t-1", "a1-s1", 3)


def _proc_count(image: str) -> int:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        capture_output=True,
        text=True,
    )
    return out.stdout.lower().count(image.lower())


@pytest.mark.skipif(not _proc_count, reason="windows tasklist required")
def test_terminal_timeout_kills_orphan(tmp_path):
    if (
        _proc_count("ping.exe") >= 0
        and subprocess.run(["where.exe", "ping"], capture_output=True).returncode != 0
    ):
        pytest.skip("ping unavailable")
    gw, _, _ = _stack(tmp_path)
    before = _proc_count("ping.exe")
    kind, res = _attempt(
        gw,
        "terminal",
        {"command": "ping", "args": ["-n", "30", "127.0.0.1"], "timeout_seconds": 0.6},
    )
    assert res.success is False and res.error_code == "COMMAND_TIMEOUT"
    time.sleep(0.5)
    assert _proc_count("ping.exe") <= before


# --------------------------------------------------------------------- web ---
def test_web_private_localhost_structured_denial(tmp_path):
    gw, _, _ = _stack(tmp_path)
    kind, res = _attempt(gw, "web.fetch", {"url": "https://localhost:11434/x"})
    # tool policy allows web.fetch; the NETWORK layer refuses private hosts,
    # and the web tool converts that into a STRUCTURED failure (not an
    # exception escaping the tool).
    assert kind == "exec"
    assert res.success is False
    assert res.error_code == "NETWORK_DENIED"


def test_web_success_is_untrusted_data(tmp_path):
    gw, _, _ = _stack(tmp_path)
    kind, res = _attempt(gw, "web.fetch", {"url": "https://ok.test/page"})
    assert kind == "exec" and res.success
    assert res.data["untrusted"] is True
    assert res.evidence.get("content_hash")


# ----------------------------------------------------------------- generated -
_NONZERO = (
    "# nomadicos-tool\n# description: flaky\n# arguments_schema: {}\nimport sys\nsys.exit(2)\n"
)
_INVALID = "# nomadicos-tool\n# description: flaky\n# arguments_schema: {}\nprint('not json')\n"
_SLOW = (
    "# nomadicos-tool\n# description: flaky\n# arguments_schema: {}\n"
    "import time\nprint('{}')\ntime.sleep(30)\n"
)


def test_generated_nonzero_exit_structured(tmp_path):
    gw, _, _ = _stack(tmp_path, extra_script=_NONZERO)
    kind, res = _attempt(gw, "script.flaky", {})
    assert kind == "exec" and res.success is False
    assert res.error_code == "SCRIPT_NONZERO_EXIT"
    assert res.evidence.get("exit_code") == 2


def test_generated_invalid_output_raise_code(tmp_path):
    gw, _, _ = _stack(tmp_path, extra_script=_INVALID)
    kind, res = _attempt(gw, "script.flaky", {})
    assert res.success is False and res.error_code == "SCRIPT_INVALID_OUTPUT"


def test_generated_timeout_kills_child(tmp_path, monkeypatch):
    import nomadicos.tools.generated as genmod

    monkeypatch.setattr(genmod, "SCRIPT_TIMEOUT_SECONDS", 0.6, raising=False)
    monkeypatch.setattr(genmod, "SCRIPT_KILL_GRACE_SECONDS", 2.0, raising=False)
    gw, _, _ = _stack(tmp_path, extra_script=_SLOW)
    before = _proc_count("python.exe")
    kind, res = _attempt(gw, "script.flaky", {})
    assert res.success is False and res.error_code == "SCRIPT_TIMEOUT"
    time.sleep(0.5)
    assert _proc_count("python.exe") <= before


# --------------------------------------------------------- matrix invariant --
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("filesystem", {"action": "read", "path": "C:\\Windows\\win.ini"}),
        ("terminal", {"command": "no-such-binary-here"}),
    ],
)
def test_every_failure_carries_error_code(tmp_path, tool, arguments):
    gw, _, _ = _stack(tmp_path)
    kind, res = _attempt(gw, tool, arguments)
    assert res.success is False
    assert res.error_code, f"{tool}: failure without machine-readable code"
    assert res.task_id and res.step_id
    if kind == "exec":
        assert res.attempt == 3
