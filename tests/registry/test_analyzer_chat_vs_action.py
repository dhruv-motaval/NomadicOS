"""Focused regression coverage: CHAT vs ACTION classification.

The bug: the CHAT fast-path treated "no completion predicates" as sufficient
evidence of a chat task, so actionable imperative commands ("Open Google
Chrome.", "Press Ctrl+L.") silently completed without entering the execution
pipeline. The owner rule is absolute:

    NO PREDICATES does NOT imply CHAT.

CHAT is informational/conversational intent with no external side effect;
ACTION is any request that changes or observes external state. The fix is
anchored imperative detection in the analyzer: the action verb OPENS the
sentence (optionally behind a polite lead-in); a bare verb anywhere in the
text never classifies (questions start with a wh-/how word).
"""

from __future__ import annotations

import pytest

from nomadicos.contracts.core import Goal
from nomadicos.contracts.model import CapabilityTag, TaskType
from nomadicos.router.analyzer import analyze_goal


def Ggoal(text: str) -> Goal:  # noqa: N802 - helper
    return Goal(objective=text)


# ------------------------------------------------------------- chat cases --


@pytest.mark.parametrize(
    "objective",
    [
        "What is 2 + 2?",
        "What is the capital of Japan?",
        # action verbs INSIDE a question are never imperative commands
        "What time does the store open?",
        "How do I start a business?",
        "What type of music is this?",
        "When does the store close?",
        "Briefly explain what a unit test is. Do not execute tools or modify files.",
    ],
)
def test_informational_questions_stay_chat(objective: str) -> None:
    req = analyze_goal(Ggoal(objective))
    assert req.task_type is TaskType.CHAT
    assert req.capabilities == [CapabilityTag.TEXT]


# ----------------------------------------------------------- action cases --


# ----------------------------------------------------------- action cases --


@pytest.mark.parametrize(
    "objective",
    [
        "Open Google Chrome.",
        "Open Google Chrome and open the MC Squares YouTube channel.",
        "Press Ctrl+L.",
        "Can you press 'win + d' key on keyboard",
        "Please open the notes app.",
        "Could you launch Chrome?",
        "Close the window.",
        "Type hello into the search box.",
        # find/locate/list/show observe external state: ACTION, never chat
        "can you find folder named NomadicOs",
        "can you find folder named NomadicOs, and open it in file explorer",
        "Please locate the config file.",
        "List all running processes.",
        "Show me the current directory.",
    ],
)
def test_imperative_action_requests_are_action_not_chat(objective: str) -> None:
    # actionable desktop/desktop-adjacent imperatives enter the normal
    # execution pipeline (TERMINAL: text + tool_use), never the CHAT
    # completion path
    req = analyze_goal(Ggoal(objective))
    assert req.task_type is TaskType.TERMINAL
    assert CapabilityTag.TOOL_USE in req.capabilities


@pytest.mark.parametrize(
    "objective",
    [
        "What did you find?",
        "How can I find peace?",
        "What does this list contain?",
        "Can you tell me what OpenAI is?",
        "Can you explain quantum computing?",
    ],
)
def test_verb_after_non_action_leadin_stays_chat(objective: str) -> None:
    # "tell"/"explain" are not action verbs and questions keep a wh-/how
    # opener: find/list INSIDE a question never classifies as action
    req = analyze_goal(Ggoal(objective))
    assert req.task_type is TaskType.CHAT


def test_create_test_txt_is_file_action_not_chat() -> None:
    req = analyze_goal(Ggoal("Create test.txt."))
    assert req.task_type is TaskType.SIMPLE_FILE
    assert CapabilityTag.TOOL_USE in req.capabilities


def test_run_pytest_is_testing_action() -> None:
    req = analyze_goal(Ggoal("Run pytest."))
    assert req.task_type is TaskType.TERMINAL
    assert CapabilityTag.TESTING in req.capabilities
    assert CapabilityTag.TOOL_USE in req.capabilities


def test_analyzer_chat_vs_action_deterministic() -> None:
    a = analyze_goal(Ggoal("Open Google Chrome."))
    b = analyze_goal(Goal(objective="Open Google Chrome."))
    assert a == b
