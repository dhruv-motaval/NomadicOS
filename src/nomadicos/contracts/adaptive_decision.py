"""Adaptive decision contracts (Phase 4D, SPEC §9).

Typed, closed schemas for the adaptive orchestration decision cycle.
Models propose decisions; NomadicOS authorizes and the runtime validates.
Model-authored authority fields are rejected by extra="forbid" (SPEC §19).

Every AdaptiveDecision is:
- untrusted data until validated by trusted runtime code (this module),
- attributable to a mission/task via correlation fields,
- bounded by retry/replan budgets tracked in trusted state,
- never able to grant itself permissions, change the mission objective,
  mark the mission successful, or bypass authorization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.contracts.verification import EvidenceItem


class Decision(StrEnum):
    """Adaptive decision outcomes (SPEC §4D)."""

    CONTINUE = "CONTINUE"  # proceed with currently eligible work
    RETRY = "RETRY"  # retry a specific eligible task within authorized budget
    REPLAN = "REPLAN"  # propose a revised plan for unfinished work
    ESCALATE = "ESCALATE"  # request more info, capability, or owner approval
    STOP = "STOP"  # terminate mission without declaring success


class DecisionCategory(StrEnum):
    """Why a decision was made — trusted taxonomy, never model-invented."""

    PLAN_COMPLETE = "plan_complete"  # all eligible tasks done
    DEPENDENCY_UNMET = "dependency_unmet"  # prerequisite not satisfied
    TASK_FAILED = "task_failed"  # last task result was FAILED
    TASK_TIMED_OUT = "task_timed_out"  # last task hit wall-clock budget
    TASK_REJECTED = "task_rejected"  # last worker/model output rejected
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"  # verification needs more
    AUTHORIZATION_DENIED = "authorization_denied"  # owner/authz blocked action
    OUTCOME_UNKNOWN = "outcome_unknown"  # side-effect durability unproven
    BUDGET_EXHAUSTED = "budget_exhausted"  # retries/replans/decisions spent
    CRITIC_REJECT = "critic_reject"  # critic rejected implementation
    OWNER_REQUEST = "owner_request"  # explicit owner stop/escalate
    MODEL_ERROR = "model_error"  # brain/model proposal failure
    RESOURCE_UNAVAILABLE = "resource_unavailable"  # engine/tool/resource missing
    INTERNAL = "internal"  # runtime/bookkeeping reason


class AdaptiveDecision(Contract):
    """One adaptive decision: typed, closed, attributable, bounded.

    Models propose decisions; NomadicOS authorizes; the runtime validates
    before committing any state change.  Authority-shaped fields are
    rejected by extra="forbid" (SPEC §19).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- decision ---
    decision: Decision
    category: DecisionCategory
    rationale: str  # bounded human-readable why

    # --- identity / correlation (§13, §56.6) ---
    mission_id: str
    task_id: str | None = None  # the task this decision concerns, if any
    step_id: str | None = None
    correlation_id: str  # links this decision to its parent run/task
    correlation_type: str = "mission"  # "mission" or "task"

    # --- evidence (untrusted until validated) ---
    supporting_evidence: list[EvidenceItem] = Field(default_factory=list)
    expected_effect: str = ""  # what this decision expects to change

    # --- plan revision (REPLAN only) ---
    plan_revision: int | None = None  # new plan revision number, if REPLAN

    # --- budget context (trusted, re-checked by runtime) ---
    retries_used: int = 0
    retries_remaining: int = 0
    replans_used: int = 0
    replans_remaining: int = 0
    decisions_used: int = 0
    decisions_remaining: int = 0

    # --- timing ---
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _bounded(self) -> AdaptiveDecision:
        if len(self.rationale) > 400:
            raise ValueError("rationale exceeds 400 chars")
        if len(self.expected_effect) > 200:
            raise ValueError("expected_effect exceeds 200 chars")
        if self.retries_used < 0 or self.retries_remaining < 0:
            raise ValueError("retry counts must be non-negative")
        if self.replans_used < 0 or self.replans_remaining < 0:
            raise ValueError("replan counts must be non-negative")
        if self.decisions_used < 0 or self.decisions_remaining < 0:
            raise ValueError("decision counts must be non-negative")
        # REPLAN must carry a plan_revision; others must not
        if self.decision is Decision.REPLAN and self.plan_revision is None:
            raise ValueError("REPLAN decision requires a plan_revision")
        if self.decision is not Decision.REPLAN and self.plan_revision is not None:
            raise ValueError("plan_revision is only meaningful for REPLAN")
        return self


class AdaptiveContext(Contract):
    """Trusted snapshot of runtime state fed to the adaptive decision cycle.
    Built by the orchestrator from TaskState — never from model output.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    task_id: str | None
    step_id: str | None
    plan_revision: int
    task_status: str  # TaskStatus value
    task_status_map: dict[str, str]  # task node id -> status
    completed_tasks: list[str]
    failed_tasks: list[str]
    blocked_tasks: list[str]
    pending_tasks: list[str]
    last_failure_category: str | None
    last_failure_message: str | None
    last_execution_succeeded: bool | None
    last_verdict: str | None  # VerificationOutcome value for goal
    last_step_verdict: str | None  # VerificationOutcome value for step
    retries_used: int
    retries_remaining: int
    replans_used: int
    replans_remaining: int
    decisions_used: int
    decisions_remaining: int
    max_retries_per_task: int
    max_replans_per_mission: int
    max_adaptive_decisions_per_mission: int
    max_reasoning_calls_per_cycle: int
    outcome_uncertain: bool
    previous_decisions: list[dict[str, Any]] = Field(default_factory=list)
    #: trusted memory references (read-only, never authority)
    memory_references: list[str] = Field(default_factory=list)
    #: model/engine budget remaining for this cycle
    reasoning_calls_available: int
