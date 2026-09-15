"""Core typed contracts: tasks, goals, predicates, plans (SPEC §18, §27-29)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nomadicos.kernel.errors import Failure
from nomadicos.kernel.ids import new_id


class Contract(BaseModel):
    """Base for payload contracts: closed schemas, never silent extras."""

    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    """SPEC §17 graph outcomes. SUCCESS only via goal verification (§28)."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    RECOVERING = "RECOVERING"
    WAITING_OWNER = "WAITING_OWNER"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class LogicalOp(StrEnum):
    ALL = "all"
    ANY = "any"
    NOT = "not"


class Predicate(Contract):
    """One observable completion condition (SPEC §29).

    Unknown predicate types are rejected at evaluation time, never treated as
    satisfied (fail closed).
    """

    type: str
    fields: dict[str, Any] = Field(default_factory=dict)

    def field(self, name: str, default: Any = None) -> Any:
        return self.fields.get(name, default)


class PredicateGroup(Contract):
    op: LogicalOp
    children: list[GoalPredicate]

    @model_validator(mode="after")
    def _bounded(self) -> PredicateGroup:
        if self.op is LogicalOp.NOT:
            if len(self.children) != 1:
                raise ValueError("NOT requires exactly one child predicate")
        elif not self.children:
            raise ValueError("predicate group must not be empty")
        if len(self.children) > 16:
            raise ValueError("predicate group exceeds bounded width of 16 children")
        return self


class GoalPredicate(Contract):
    """Either a leaf predicate or a recursive all/any group (SPEC §29)."""

    predicate: Predicate | None = None
    group: PredicateGroup | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> GoalPredicate:
        if (self.predicate is None) == (self.group is None):
            raise ValueError("GoalPredicate requires exactly one of predicate|group")
        return self


def parse_predicate(expr: Any) -> GoalPredicate:
    """Parse nested {"all": [...]} / {"any": [...]} / {"type": ...} syntax."""
    if isinstance(expr, str):
        return GoalPredicate(predicate=Predicate(type=expr))
    if isinstance(expr, dict):
        if "type" in expr:
            if set(expr) & {"all", "any", "not"}:
                raise ValueError("predicate cannot mix a type with logical operators")
            fields = {k: v for k, v in expr.items() if k != "type"}
            return GoalPredicate(predicate=Predicate(type=str(expr["type"]), fields=fields))
        if len(expr) != 1:
            raise ValueError("predicate group must have exactly one operator key")
        ((op, children),) = expr.items()
        if op == "not":
            single = children if isinstance(children, list) else [children]
            return GoalPredicate(
                group=PredicateGroup(op=LogicalOp.NOT, children=[parse_predicate(single[0])])
            )
        if op not in {"all", "any"}:
            raise ValueError(f"unknown predicate operator {op!r}")
        if not isinstance(children, list) or not children:
            raise ValueError(f"{op!r} requires a non-empty list of predicates")
        return GoalPredicate(
            group=PredicateGroup(op=LogicalOp(op), children=[parse_predicate(c) for c in children])
        )
    raise ValueError(f"cannot parse predicate from {type(expr).__name__}")


class Goal(Contract):
    """The owner objective: what must become true (§27), never model-rewritable.

    FROZEN (SPEC §8.14): recovery/replanning must never mutate the goal that
    defines success - only a fresh owner instruction creates a new goal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("task"))
    objective: str
    #: Owner constraints, e.g. "Do not touch Project B" (SPEC §5, §8).
    constraints: list[str] = Field(default_factory=list)
    #: Completion conditions. Success requires these to verifiably hold.
    predicates: list[GoalPredicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _objective_nonempty(self) -> Goal:
        if not self.objective.strip():
            raise ValueError("goal objective must not be empty")
        return self

    @staticmethod
    def from_spec(
        objective: str,
        *,
        constraints: list[str] | None = None,
        predicates: list[Any] | None = None,
    ) -> Goal:
        return Goal(
            objective=objective,
            constraints=constraints or [],
            predicates=[parse_predicate(p) for p in (predicates or [])],
        )


class PlanStep(Contract):
    id: str = Field(default_factory=lambda: new_id("step"))
    description: str
    #: Optional per-step expectation used by the step verifier (SPEC §27).
    expected: GoalPredicate | None = None

    @model_validator(mode="after")
    def _description_nonempty(self) -> PlanStep:
        if not self.description.strip():
            raise ValueError("plan step description must not be empty")
        return self


class Plan(Contract):
    task_id: str
    steps: list[PlanStep]
    revised_count: int = 0


class FailureRecord(Contract):
    """Explicit failure with taxonomy (SPEC §30). Never coerced to success."""

    id: str = Field(default_factory=lambda: new_id("task"))
    category: Failure
    message: str
    task_id: str
    step_id: str | None = None
    attempt: int | None = None
    model_id: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
