"""Structured coding plan (SPEC §9.6, §9.21).

Deterministic plan derived ONLY from the owner goal + its completion
predicates: inspect -> (git state) -> per-file implement -> test.
The goal object is never rewritten; repair lives in the graph's recovery.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nomadicos.contracts.core import Goal, GoalPredicate, Plan, PlanStep

MAX_CODING_STEPS = 12


def flatten_predicates(goal: Goal) -> list[GoalPredicate]:
    out: list[GoalPredicate] = []

    def walk(item: Any) -> None:
        pred = getattr(item, "predicate", None)
        group = getattr(item, "group", None)
        if pred is not None:
            out.append(item)
        if group is not None:
            for child in group.children:
                walk(child)

    for p in goal.predicates:
        walk(p)
    return out


def _file_targets(goal: Goal) -> list[tuple[str, GoalPredicate]]:
    """Leaf predicates bound to a concrete file target (owner-authored)."""
    out: list[tuple[str, GoalPredicate]] = []
    seen: set[str] = set()
    for gp in flatten_predicates(goal):
        leaf = gp.predicate
        if leaf is None:
            continue
        path = leaf.field("path")
        if isinstance(path, str) and path and path not in seen:
            seen.add(path)
            out.append((path, gp))
    return out


class CodingPlanner:
    """Emits inspect/implement/test work steps from the owner goal."""

    def __init__(self, *, has_git: bool = False) -> None:
        self.has_git = has_git

    def plan(self, goal: Goal, requirements: Any) -> Plan:
        steps: list[PlanStep] = []
        steps.append(
            PlanStep(
                description=(
                    "Inspect the repository: propose filesystem.list of '.' and read "
                    "the files relevant to the objective (bounded, no full dumps)."
                )
            )
        )
        if self.has_git:
            steps.append(
                PlanStep(
                    description=(
                        "Check repository state: propose terminal.execute with "
                        'command "git" and args ["status","--porcelain"] (read-only).'
                    )
                )
            )
        for path, gp in _file_targets(goal)[:8]:
            leaf = gp.predicate
            leaf_desc = f"{leaf.type}:{leaf.fields}" if leaf is not None else "condition group"
            steps.append(
                PlanStep(
                    description=(
                        f"Make the minimal targeted change so completion condition "
                        f"[{leaf_desc}] for '{path}' holds. "
                        "Modify only files this requires."
                    ),
                    expected=gp,
                )
            )
        for gp in flatten_predicates(goal):
            leaf = gp.predicate
            if leaf is not None and leaf.type in {"tests_pass", "exit_code_equals"}:
                steps.append(
                    PlanStep(
                        description=(
                            "Run the repository test command exactly as specified in the "
                            "completion condition (terminal.execute); capture exit code."
                        ),
                        expected=gp,
                    )
                )
        if not any(
            (
                s.expected is not None
                and s.expected.predicate is not None
                and s.expected.predicate.type == "tests_pass"
            )
            for s in steps
        ):
            steps.append(
                PlanStep(
                    description=(
                        "Verify: state remaining conditions; when the implementation "
                        'matches every completion predicate reply {"finished": true}.'
                    )
                )
            )
        return Plan(task_id=goal.id, steps=steps[:MAX_CODING_STEPS])


def predicates_for_coding_task(
    *,
    test_command: str | None,
    require_files: Iterable[str] = (),
    require_content: Iterable[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """OWNER-authored completion predicates for `nomadicos code` (SPEC §9.21).
    Values come from the owner's invocation, never from a worker/model."""
    preds: list[dict[str, Any]] = []
    for path in require_files:
        preds.append({"type": "file_exists", "path": path})
    for path, needle in require_content:
        preds.append({"type": "file_contains", "path": path, "text": needle})
    if test_command:
        preds.append({"type": "tests_pass", "command": test_command})
    return preds
