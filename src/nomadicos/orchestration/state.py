"""Typed LangGraph task state (SPEC §18).

Rules enforced by structure, not convention:

- NO authority booleans anywhere. An authorization exists only as an
  ``AuthorizedAction`` artifact held by the runtime's issued-artifact table;
  state stores mere id references (``pending_authorized_id``).
- Histories are append-only (reducer) lists; scalar flow fields are
  overwritten only by their owning node (SPEC §7.5-7.16 ownership rules).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from nomadicos.contracts.action import ActionProposal
from nomadicos.contracts.core import FailureRecord, Goal, PlanStep, TaskStatus
from nomadicos.contracts.execution import ExecutionResult, Observation
from nomadicos.contracts.model import TaskRequirements
from nomadicos.contracts.verification import VerificationResult


class TaskState(TypedDict, total=False):
    # — identity (INTAKE owns; INTAKE consumes goal_text input)
    task_id: str
    goal_text: str
    goal_constraints: list[str]
    goal_predicates: list[dict[str, Any]]
    goal: Goal

    # — routing context (CLASSIFY owns)
    requirements: TaskRequirements

    # — plan (PLAN / REPLAN owns)
    plan: list[PlanStep]
    current_step: int

    # — model selection (SELECT MODEL owns)
    model_id: str | None
    #: plain list (overwrite): escalation ledger per task, reset on advance
    tried_models: list[str]
    escalation_count: Annotated[list[dict[str, Any]], operator.add]

    # — attempt / budget bookkeeping (owning nodes only)
    attempt: int
    recovery_count: Annotated[list[dict[str, Any]], operator.add]
    total_steps_used: int

    # — IR artifacts (referenced proposals; never authority)
    proposals: Annotated[list[ActionProposal], operator.add]
    completion_claim: bool
    #: capability dict resolved by VALIDATE, consumed by AUTHORIZE
    resolved_capability: dict[str, Any] | None
    #: last typed failure summary (category + message) for routing honesty
    last_failure: dict[str, Any] | None

    # — authorization reference ONLY (AUTHORIZE owns; execute looks up in the
    #   runtime table — state can never carry or mint the artifact itself)
    pending_authorized_id: str | None
    #: set by EXECUTE when the executor refused before producing a result
    execution_missing: bool

    # — histories (append via reducers, bounded by trimming in nodes)
    observations: Annotated[list[Observation], operator.add]
    executions: Annotated[list[ExecutionResult], operator.add]
    verifications: Annotated[list[VerificationResult], operator.add]
    failures: Annotated[list[FailureRecord], operator.add]

    # — owner-conflict interrupt plumbing (AUTHORIZE/OWNER WAIT owns)
    pending_conflict: dict[str, Any] | None

    # — lifecycle
    task_status: TaskStatus
    outcome_note: str

    # — per-step fingerprints for stuck detection (§31)
    fingerprint_history: Annotated[list[str], operator.add]


STATE_FIELDS: tuple[str, ...] = tuple(TaskState.__annotations__)

#: state must never contain these — authority is an artifact, not a bool
FORBIDDEN_STATE_KEYS = frozenset(
    {"authorized", "allowed", "approved", "permission", "grant", "bypass"}
)


def assert_no_authority_keys(update: dict[str, Any]) -> None:  # pragma: no cover - defensive
    leak = FORBIDDEN_STATE_KEYS & {k.lower() for k in update}
    if leak:
        raise AssertionError(f"authority-shaped state key attempted: {sorted(leak)}")
