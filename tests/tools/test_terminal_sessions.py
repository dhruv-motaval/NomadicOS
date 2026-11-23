"""Persistent terminals (terminal.open / send / read / close)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomadicos.authority import AuthorityStore, AuthorizationService, CapabilityPolicy
from nomadicos.contracts.action import ActionProposal
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.executor.dispatch import Executor
from nomadicos.kernel.events import EventLogger
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import PathDenied
from nomadicos.tools.terminal import TerminalTool

REPL = {"command": "python", "args": ["-u", "-i"]}


def ctx(tmp_path: Path, task: str = "agent-a") -> ExecutionContext:
    return ExecutionContext.for_task(task, tmp_path / "ws")


async def read_until(tool: TerminalTool, c: ExecutionContext, session: str, needle: str) -> str:
    seen = ""
    for _ in range(20):
        out = await tool.run("read", {"session": session, "wait_s": 1.0}, c)
        seen += out.stdout
        if needle in seen:
            break
    return seen


async def test_terminal_stays_open_between_commands(tmp_path: Path) -> None:
    tool, c = TerminalTool(), ctx(tmp_path)
    opened = await tool.run("open", REPL, c)
    assert opened.status is ExecutionStatus.SUCCEEDED
    session = opened.evidence["session"]
    try:
        await tool.run("send", {"session": session, "text": "x = 6"}, c)
        await tool.run("send", {"session": session, "text": "print(x * 7)"}, c)
        assert "42" in await read_until(tool, c, session, "42")  # state kept across sends
        again = await tool.run("read", {"session": session, "wait_s": 0.2}, c)
        assert "42" not in again.stdout  # a read returns only new output
    finally:
        await tool.run("close", {"session": session}, c)
    gone = await tool.run("send", {"session": session, "text": "print(1)"}, c)
    assert gone.status is ExecutionStatus.FAILED


async def test_terminals_belong_to_the_agent_that_opened_them(tmp_path: Path) -> None:
    tool = TerminalTool()
    mine, other = ctx(tmp_path, "agent-a"), ctx(tmp_path, "agent-b")
    session = (await tool.run("open", REPL, mine)).evidence["session"]
    try:
        peek = await tool.run("read", {"session": session, "wait_s": 0}, other)
        assert peek.status is ExecutionStatus.FAILED
    finally:
        killed = tool.end_task("agent-a")  # the agent's run ended
    assert killed
    assert (await tool.run("read", {"session": session}, mine)).status is ExecutionStatus.FAILED


async def test_typed_text_is_screened_for_owner_state(tmp_path: Path) -> None:
    tool, c = TerminalTool(), ctx(tmp_path)
    session = (await tool.run("open", REPL, c)).evidence["session"]
    try:
        with pytest.raises(PathDenied):
            await tool.run("send", {"session": session, "text": "open('authority.json')"}, c)
    finally:
        tool.end_task(c.task_id)


async def test_repeated_reads_are_not_blocked_as_duplicate_actions(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "authority.json")
    store.grant_full_pc_autonomy()
    log = EventLogger()
    tools = ToolRegistry()
    terminal = TerminalTool()
    tools.register(terminal)
    authz = AuthorizationService(
        store, CapabilityPolicy(store, granted_patterns=["terminal.*"]), log
    )
    executor = Executor(tools, store, log)
    c = ctx(tmp_path)

    async def act(operation: str, args: dict):
        from nomadicos.contracts.action import CapabilityRef

        proposal = ActionProposal(
            task_id=c.task_id,
            step_id="s",
            model_id="m",
            tool="terminal",
            operation=operation,
            args=args,
        )
        granted = authz.authorize(proposal, CapabilityRef(capability=f"terminal.{operation}"))
        return await executor.execute(granted, c)

    session = (await act("open", REPL)).evidence["session"]
    try:
        for _ in range(2):  # the same read twice in one step is a normal thing to do
            result = await act("read", {"session": session, "wait_s": 0.1})
            assert not result.evidence.get("duplicate_blocked")
    finally:
        executor.cleanup_task(c.task_id)
