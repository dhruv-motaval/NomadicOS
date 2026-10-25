"""Focused regression coverage: CHAT completion vs ACTION pipeline routing.

Guarantees (owner rule: NO PREDICATES does NOT imply CHAT):

- a genuine CHAT goal answers via a completion claim with ZERO executor
  calls and never shows the tool catalog;
- an ACTION goal (classified TERMINAL by the analyzer) never enters the
  CHAT completion path: its prompts carry the REGISTERED ACTIONS catalog
  and its proposals flow through validate -> authorize -> execute.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_app, write_json  # tests/orchestration

from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.contracts.model import CapabilityTag, TaskRequirements
from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.tools import DesktopTool


def desktop_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "desktop", "operation": operation, "args": args})


def make_desktop_app(tmp_path: Path, scripts: list[tuple[str, str]]):
    """make_app + a deterministic FakeDesktopBackend through the normal
    registration path (no production component modified for tests)."""
    app = make_app(tmp_path, scripts=scripts)
    app.tools.register(DesktopTool(FakeDesktopBackend(width=800, height=600)))
    return app


def first_prompt(app) -> str:
    return "\n".join(m.content for m in app.mock.calls[0].messages)


async def test_chat_goal_answers_without_executor_calls(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[("What is 2 + 2?", '{"finished": true, "justification": "4"}')],
    )
    summary = await app.run_goal("What is 2 + 2?")
    assert summary.executions == 0
    assert "4" in summary.outcome_note
    # the CHAT completion path was used: no tool catalog in the prompt
    prompt = first_prompt(app)
    assert "REGISTERED ACTIONS" not in prompt
    assert "Answer the user's question directly" in prompt


async def test_action_goal_does_not_enter_chat_completion_path(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=[("Open Google Chrome", write_json("out.txt", "hi"))])
    summary = await app.run_goal("Open Google Chrome.")
    prompt = first_prompt(app)
    # normal proposal pipeline: the registered-action catalog IS shown and
    # the chat-only completion script is NOT used
    assert "REGISTERED ACTIONS" in prompt
    assert "Answer the user's question directly" not in prompt
    # the scripted action flowed through validate -> authorize -> execute
    assert summary.executions >= 1


async def test_action_goal_reaches_execution_pipeline(tmp_path: Path) -> None:
    app = make_desktop_app(
        tmp_path,
        # two-step plan: canonical action once, then a compliant claim
        scripts=[
            (
                "Ctrl",
                [desktop_json("hotkey", {"keys": ["ctrl", "l"]}), '{"finished": true}'],
            )
        ],
    )
    summary = await app.run_goal("Press Ctrl+L.")
    backend = app.tools.get("desktop").backend
    assert backend.hotkeys == [["ctrl", "l"]]
    assert summary.executions >= 1
    assert summary.status in (TaskStatus.PARTIAL, TaskStatus.SUCCESS)
    # and the chat completion path was never taken
    assert "REGISTERED ACTIONS" in first_prompt(app)


def test_action_goal_without_predicates_gets_multi_step_plan(tmp_path: Path) -> None:
    """'find X, then open it' is multi-phase work: a ONE-step plan ends the
    task after a single proposal. Non-predicate ACTION goals get a bounded
    investigate -> act scaffold so at least two proposal turns exist."""
    from nomadicos.contracts.model import TaskType
    from nomadicos.orchestration.planner import StructuralPlanner

    goal_text = "can you find folder named NomadicOs, and open it in file explorer"
    plan = StructuralPlanner().plan(
        Goal(objective=goal_text),
        TaskRequirements(
            task_type=TaskType.TERMINAL,
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE],
            difficulty=0.3,
        ),
    )
    assert len(plan.steps) == 2
    assert plan.steps[0].description.startswith("Investigate")
    assert "Act:" in plan.steps[1].description
    # chat keeps its single answer step
    chat_plan = StructuralPlanner().plan(
        Goal(objective="what is 2 + 2"),
        TaskRequirements(
            task_type=TaskType.CHAT, capabilities=[CapabilityTag.TEXT], difficulty=0.2
        ),
    )
    assert len(chat_plan.steps) == 1
    assert chat_plan.steps[0].description.startswith("Answer the user's question")
