"""Deterministic task analyzer (SPEC §13).

Same input ⇒ same requirements. Heuristic keyword scoring only; no model is
needed to analyze a task.
"""

from __future__ import annotations

import re

from nomadicos.contracts.core import Goal
from nomadicos.contracts.model import CapabilityTag, TaskRequirements, TaskType
from nomadicos.kernel.config import RoutingMode

_CODING = re.compile(
    r"\b(code|bug|fix|implement|function|class|module|refactor|api|"
    r"syntax|parser|compile|error|exception|repository|repo|feature)\b",
    re.IGNORECASE,
)
_SIMPLE_FILE = re.compile(
    r"\b(rename|move|copy|delete|remove|list (files|directory)|read file|write file|"
    r"create file|touch|mkdir)\b"
    # filename-creation phrasing ("create test.txt", "create benchmark_output.txt"):
    # a file target with an extension is file work, never coding/testing
    r"|\b(create|write|touch)\s+[\w./\\-]+\.\w+\b",
    re.IGNORECASE,
)
# Explicit testing intent: a mention of a test is not itself a request to
# perform testing. Keep
# explanatory prompts ("what is a unit test?") in the CHAT path; require an
# explicit testing verb or an imperative "test …" request.  Predicates still
# independently establish testing intent below.
_TESTING_INTENT = re.compile(
    r"\b(?:run|execute|make|fix|repair|write|add)\b[^.]{0,80}\b(?:tests?|pytest)\b"
    r"|^\s*test\b(?!\.\w)",
    re.IGNORECASE,
)
_TERMINAL = re.compile(
    r"\b(run|execute|command|shell|script|install|npm|git status|press\b.*\bkey|hotkey)\b",
    re.IGNORECASE,
)
_NEGATED_ACTION = re.compile(
    r"\b(?:do\s+not|don't|never)\s+(?:run|execute|modify|write|create|delete|remove|"
    r"open|launch|click|type|press)\b",
    re.IGNORECASE,
)
# imperative ACTION requests ("open chrome", "press ctrl+l", "find the
# folder", "close the window"): the action verb OPENS the sentence
# (imperative mood), optionally behind a polite lead-in. A bare verb anywhere
# in the text would misclassify questions ("what time does the store open?"
# is chat; "Open Chrome." is action; "can you find folder X" is an external-
# state lookup, not chat). NO PREDICATES does NOT imply CHAT: an actionable
# imperative must enter the normal execution pipeline even without
# completion predicates. find/locate/list/show/read observe external state;
# open/launch/click/type/press change it.
_DESKTOP_ACTION = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+|kindly\s+)*"
    r"\b(open|launch|start|focus|switch|activate|restore|minimize|maximize|"
    r"close|quit|exit|click|type|press|hotkey|navigate|browse|scroll|hover|"
    r"select|search|kill|find|locate|list|show|display|read)\b",
    re.IGNORECASE,
)
_HARD = re.compile(
    r"\b(architecture|multi-module|multi-file|complex|distributed|concurrent)\b", re.IGNORECASE
)
_LARGE_CONTEXT = re.compile(
    r"\b(entire codebase|whole project|multi-file|across modules|large repo)\b", re.IGNORECASE
)
_CLASSIFY = re.compile(r"\b(classify|categorize|label|summarize)\b", re.IGNORECASE)


def _contains_leaf_types(goal: Goal) -> list[str]:
    types: list[str] = []

    def walk(expr: object) -> None:
        gp = getattr(expr, "predicate", None)
        grp = getattr(expr, "group", None)
        if gp is not None:
            types.append(gp.type)
        if grp is not None:
            for child in grp.children:
                walk(child)

    for pred in goal.predicates:
        walk(pred)
    return types


def analyze_goal(goal: Goal, *, base_latency: RoutingMode = "BALANCED") -> TaskRequirements:
    text = goal.objective
    types = _contains_leaf_types(goal)
    difficulty = 0.2
    task_type = TaskType.CHAT
    caps: list[CapabilityTag] = [CapabilityTag.TEXT]

    if _CODING.search(text) or "tests_pass" in types or "repair" in text.lower():
        task_type = TaskType.CODING
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
        difficulty = 0.5
    elif _SIMPLE_FILE.search(text):
        task_type = TaskType.SIMPLE_FILE
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
        difficulty = 0.15
    elif _TERMINAL.search(text) and not _NEGATED_ACTION.search(text):
        task_type = TaskType.TERMINAL
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
        difficulty = 0.3

    # explicit testing intent: a genuine testing request ("run tests",
    # "execute pytest", "make the tests pass", "fix the failing tests")
    # adds TESTING wherever the task type landed; a filename token
    # (test.txt) never does. On a chat-shaped goal the intent is real work:
    # the previous analyzer classified such goals CODING.
    if _TESTING_INTENT.search(text) or "test" in types:
        if task_type is TaskType.CHAT:
            task_type = TaskType.CODING
            caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
            difficulty = max(difficulty, 0.5)
        caps.append(CapabilityTag.TESTING)

    # imperative action requests ("open chrome", "press ctrl+l"): verb-first
    # commands are ACTION work regardless of completion predicates — no
    # predicates does NOT imply chat (questions start with a wh-/how word
    # and never match the imperative anchor)
    if task_type is TaskType.CHAT and _DESKTOP_ACTION.search(text):
        task_type = TaskType.TERMINAL
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
        difficulty = max(difficulty, 0.3)

    if types and "chat" == task_type:
        # explicit predicates imply measurable, tool-driven work
        task_type = TaskType.TERMINAL
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]

    if _LARGE_CONTEXT.search(text):
        difficulty += 0.15
    if _HARD.search(text):
        difficulty += 0.25
    if goal.constraints:
        difficulty += 0.1
    if len(goal.predicates) > 1:
        difficulty += 0.1
    difficulty = min(difficulty, 1.0)

    if _CLASSIFY.search(text) and task_type is TaskType.CHAT:
        task_type = TaskType.CLASSIFICATION
        difficulty = min(difficulty, 0.1)

    if goal.predicates:
        caps = list(dict.fromkeys(caps + [CapabilityTag.REASONING]))

    latency: RoutingMode = "QUALITY" if difficulty >= 0.7 else base_latency
    return TaskRequirements(
        task_type=task_type,
        capabilities=caps,
        difficulty=difficulty,
        needs_large_context=bool(_LARGE_CONTEXT.search(text))
        or task_type is TaskType.CODING
        and difficulty >= 0.6,
        latency=latency,
        verification_required=bool(goal.predicates or types) or task_type is not TaskType.CHAT,
    )
