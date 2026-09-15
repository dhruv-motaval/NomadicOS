"""Typed planning (SPEC §17 PLAN node; Phase 7 scope: deterministic planner).

Planning produces ``PlanStep`` structures only — never free-form control.
A model-free structural planner: each goal predicate becomes a satisfaction
step; a goal without predicates yields a single investigate-and-act step.
Model-assisted planners are replaceable bricks (§13) injected via the same
``Planner`` protocol; graph routing is unaffected either way.
"""

from __future__ import annotations

from typing import Protocol

from nomadicos.contracts.core import Goal, GoalPredicate, Plan, PlanStep
from nomadicos.contracts.model import TaskRequirements


class Planner(Protocol):
    def plan(self, goal: Goal, requirements: TaskRequirements) -> Plan: ...


def _leaves(expr: GoalPredicate) -> list[str]:
    if expr.predicate is not None:
        t = expr.predicate.type
        detail = {k: v for k, v in expr.predicate.fields.items()}
        return [f"{t}:{detail}" if detail else t]
    if expr.group is not None:
        out: list[str] = []
        for child in expr.group.children:
            out.extend(_leaves(child))
        return out
    return []


_STEP_VERIFIABLE_TYPES = frozenset(
    {
        "file_exists",
        "directory_exists",
        "file_content_equals",
        "file_contains",
        "file_sha256",
        "artifact_exists",
        "tests_pass",
        "exit_code_equals",
        "stdout_contains",
    }
)


def _step_expectation(pred: GoalPredicate):
    """Attach the leaf predicate itself as the step's completion expectation
    when it is directly verifiable (SPEC §8.3). Groups stay informational."""
    from nomadicos.verification.predicates import SUPPORTED_TYPES

    if pred.predicate is not None and pred.predicate.type in (
        _STEP_VERIFIABLE_TYPES & SUPPORTED_TYPES
    ):
        return pred
    return None


class StructuralPlanner:
    def plan(self, goal: Goal, requirements: TaskRequirements) -> Plan:
        steps: list[PlanStep] = []
        for pred in goal.predicates:
            for leaf in _leaves(pred):
                steps.append(
                    PlanStep(
                        description=(
                            f"Satisfy completion condition [{leaf}] for objective "
                            f"{goal.objective!r} using authorized tools."
                        ),
                        expected=_step_expectation(pred),
                    )
                )
        if not steps:
            steps.append(
                PlanStep(
                    description=(
                        f"Investigate and perform the actions needed for: "
                        f"{goal.objective!r}. Then submit a completion claim."
                    )
                )
            )
        # bounded plan (SPEC §14): hard ceiling independent of model output
        return Plan(task_id=goal.id, steps=steps[:24])
