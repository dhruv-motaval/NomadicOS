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
    r"\b(code|bug|fix|implement|function|class|module|refactor|api|test|tests|pytest|"
    r"syntax|parser|compile|error|exception|repository|repo|feature)\b",
    re.IGNORECASE,
)
_SIMPLE_FILE = re.compile(
    r"\b(rename|move|copy|delete|remove|list (files|directory)|read file|write file|"
    r"create file|touch|mkdir)\b",
    re.IGNORECASE,
)
_TERMINAL = re.compile(
    r"\b(run|execute|command|shell|script|install|npm|git status)\b", re.IGNORECASE
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
        if "test" in types or _CODING.search(text):
            caps.append(CapabilityTag.TESTING)
    elif _SIMPLE_FILE.search(text):
        task_type = TaskType.SIMPLE_FILE
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
        difficulty = 0.15
    elif _TERMINAL.search(text):
        task_type = TaskType.TERMINAL
        caps = [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
        difficulty = 0.3

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
