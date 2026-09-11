"""Generated-tool contract.

Regression: a boot time mypy defect (``ToolResult.success(...)`` — a non-existent
API) meant a generated tool could never return a result. These tests execute a
real generated script end-to-end and assert the structured envelope.
"""
from pathlib import Path

from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import ToolContext
from nomadicos.tools.generated import (
    GeneratedScriptTool,
    load_generated_tools,
    save_generated_script,
)

_SCRIPT = (
    "# nomadicos-tool\n"
    "# description: echo back a marker\n"
    "# arguments_schema: {}\n"
    "import json, sys\n"
    'print(json.dumps({"summary": "ran", "data": {"marker": "ok-123"}}))\n'
)


def _ctx() -> ToolContext:
    ident = SubjectIdentity(user_id="owner", session_id="s", task_id="t")
    return ToolContext(user_id=ident.user_id, session_id=ident.session_id, task_id=ident.task_id)


def test_generated_tool_executes_and_returns_structured_result(tmp_path: Path) -> None:
    path = save_generated_script(tmp_path, "echo marker", _SCRIPT)
    assert path is not None and path.exists()

    tools = load_generated_tools(tmp_path)
    assert [t.spec.name for t in tools] == ["script.echo_marker"]

    result = _run(tools[0].execute({}, _ctx()))
    assert result.success is True
    assert result.error is None
    assert result.data["summary"] == "ran"
    assert result.data["result"]["marker"] == "ok-123"  # JSON payload surfaced
    assert "latency_ms" in result.data


def test_generated_tool_rejects_invalid_json_output(tmp_path: Path) -> None:
    bad = (
        "# nomadicos-tool\n"
        "# description: prints garbage\n"
        "# arguments_schema: {}\n"
        "print('not json')\n"
    )
    tool = GeneratedScriptTool(save_generated_script(tmp_path, "bad", bad) or Path("."))
    import pytest

    from nomadicos.core.errors import ToolExecutionError

    with pytest.raises(ToolExecutionError):
        _run(tool.execute({}, _ctx()))


def _run(coro):
    import asyncio

    return asyncio.run(coro)
