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
    completion_justification: str | None
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

    # — routing metadata ONLY (SELECT_MODEL is the sole writer; it is not
    #    authority, not completion — the verifier still decides everything)
    model_exhausted: bool

    # — worker/critic loop (CRITIQUE owns; evaluative data, never authority)
    critic_iterations: Annotated[list[dict[str, Any]], operator.add]
    critic_feedback: dict[str, Any] | None

    # — lifecycle
    task_status: TaskStatus
    outcome_note: str

    # — per-step fingerprints for stuck detection (§31)
    fingerprint_history: Annotated[list[str], operator.add]
    #: fingerprints whose execution already FAILED durably; recovery blocks
    #: an identical repeat instead of re-running a known-failed side effect
    #: (SPEC §56.14). Overwrite semantics: the owning node writes the full list.
    failed_fingerprints: list[str]

    # — Brain cognition state (Phase 1): canonical BrainState JSON, a
    #   DATA-only mirror owned by PROPOSE (init/restore) and OBSERVE
    #   (feedback). It holds candidates, monitor verdicts, and
    #   observations — never authority, never an AuthorizedAction (the
    #   runtime artifact table stays the sole source). Stored as canonical
    #   JSON so checkpoints round-trip without brain-class coupling;
    #   invalid payloads are reconstructed from authoritative data.
    brain_state: str | None

    # — Phase 2 outcome classification: set by RECOVERY when the previous
    #   execution outcome cannot be proven from durable records; recovery
    #   then STOPS (blocked) instead of replaying a possibly-executed
    #   side effect. Never a success signal.
    outcome_uncertain: bool

    # — Brain mission planning (Phase 4A): the single authoritative accepted
    #   mission plan produced by NomadicBrain's decomposer. When
    #   ``brain_planner_enabled`` is True, this replaces the legacy
    #   StructuralPlanner plan as the source of task steps. One plan per
    #   mission; never two independently mutable plans.
    mission_plan: list[PlanStep] | None
    #: Whether NomadicBrain drives mission planning for this task (opt-in
    #  per NomadicApp construction). Legacy planner path unchanged when False.
    brain_planner_enabled: bool
    #: Per-task statuses keyed by task node id. Values are TaskNodeStatus
    #: strings (PENDING, READY, ACTIVE, COMPLETED, FAILED, BLOCKED).
    #: Owned by SCHEDULE (init/advance) only.
    task_status_map: dict[str, str]
    #: Currently executing task node id, or None. Set by SCHEDULE when a
    #  task becomes ACTIVE; cleared by ADVANCE when the task reaches a
    #  terminal status.
    current_task_id: str | None
    #: Per-task execution results keyed by task node id. Written by OBSERVE
    #  after each task's executor result; read by SCHEDULE for dependency
    #  eligibility checks.
    task_results: dict[str, ExecutionResult]

    # Phase 4B worker delegation: the bounded assignment given to the
    # current specialist task (set by worker_delegate) and the structured
    # result it returns (validated before integration). Results accumulate
    # keyed by task node id and are attributable to the parent mission.
    worker_assignment: dict[str, Any] | None
    worker_result: dict[str, Any] | None
    worker_results: dict[str, dict[str, Any]]

    # Phase 4C concurrency: the set of READ-ONLY specialist task node ids
    # currently launched together (worker_fanout). Side-effecting tasks are
    # never batched. Owned by SCHEDULE (fill) and worker_fanout (clear).
    worker_batch: list[str]
    #: Trusted worker lifecycle per task node id (WorkerLifecycle values).
    #: Persisted with the checkpoint so restart reconciliation preserves
    #: worker status alongside execution status (SPEC §56.14).
    worker_lifecycle: dict[str, str]
    #: Total specialist worker launches this mission (trusted counter owned
    #: by SCHEDULE/worker nodes; enforces max_workers_per_mission).
    worker_launches: int

    # — Phase 4D adaptive decision cycle: trusted counters plus the LAST
    #   VALIDATED decision (data only — the decision never carries authority;
    #   it routes the bounded recovery loop). These fields MUST live in the
    #   typed schema: LangGraph silently drops unknown keys from node updates,
    #   and a dropped decision update would strand recovery in a retry loop
    #   that never re-enters the propose path.
    plan_revision: int
    replans_used: int
    retries_used: int
    #: per-task retry counters keyed by task node id (trusted, budget-bounded)
    task_retries: dict[str, int]
    adaptive_decisions: Annotated[list[dict[str, Any]], operator.add]
    adaptive_decisions_used: int
    adaptive_decision: dict[str, Any] | None
    last_adaptive_decision: str | None
    last_adaptive_decision_category: str | None


STATE_FIELDS: tuple[str, ...] = tuple(TaskState.__annotations__)

#: state must never contain these — authority is an artifact, not a bool
FORBIDDEN_STATE_KEYS = frozenset(
    {"authorized", "allowed", "approved", "permission", "grant", "bypass"}
)


def assert_no_authority_keys(update: dict[str, Any]) -> None:  # pragma: no cover - defensive
    leak = FORBIDDEN_STATE_KEYS & {k.lower() for k in update}
    if leak:
        raise AssertionError(f"authority-shaped state key attempted: {sorted(leak)}")
