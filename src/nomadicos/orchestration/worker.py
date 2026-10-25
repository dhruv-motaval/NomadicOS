"""Specialist worker contracts and protocol (Phase 4B, SPEC §9).

Workers are replaceable specialists that the Brain delegates bounded
subtasks to. A worker result is UNTRUSTED data until the coordinator
validates it through the normal verify step. Workers carry no authority:

- they cannot create AuthorizedAction artifacts (that is the runtime's
  authorization table alone);
- they cannot change the parent mission objective or accepted plan;
- they cannot grant themselves permissions or reuse another task's
  authorization;
- they cannot mark the overall mission successful (only the goal verifier
  can establish SUCCESS);
- they cannot silently expand their assigned scope or budget;
- they cannot launch unbounded subagent trees.

All side effects a worker needs still pass through the existing validate →
authorize → execute → observe → verify_step harness, so the durable
execution-intent ledger and cross-process claim_intent atomicity apply
unchanged.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.contracts.worker import WorkerRole, WorkerStatus


class WorkerArtifact(Contract):
    """One reference to an artifact a worker produced or touched."""

    kind: str  # "file", "command", "source", "report"
    path: str | None = None
    description: str = ""
    #: proposed file content (coding worker) or source excerpt (research).
    content: str | None = None
    sha256: str | None = None


class WorkerAssignment(Contract):
    """Bounded assignment the Brain gives a worker. Data only — no authority."""

    role: WorkerRole
    task_id: str
    mission_id: str
    step_id: str
    objective: str
    constraints: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 1

    @model_validator(mode="after")
    def _bounded(self) -> WorkerAssignment:
        if len(self.objective) > 400:
            raise ValueError("worker objective exceeds 400 chars")
        if len(self.constraints) > 16:
            raise ValueError("worker constraints exceed 16")
        if self.attempt < 1:
            raise ValueError("worker attempt must be >= 1")
        return self


class WorkerResult(Contract):
    """Structured worker output — untrusted until verified by the coordinator."""

    worker: str
    role: WorkerRole
    task_id: str
    mission_id: str
    step_id: str
    status: WorkerStatus
    artifacts: list[WorkerArtifact] = Field(default_factory=list)
    verification_evidence: list[dict[str, Any]] = Field(default_factory=list)
    failure_category: str | None = None
    diagnostics: str | None = None
    model_used: str | None = None
    actions_attempted: int = 0
    elapsed_s: float = 0.0

    @model_validator(mode="after")
    def _bounded(self) -> WorkerResult:
        if self.diagnostics and len(self.diagnostics) > 500:
            raise ValueError("worker diagnostics exceed 500 chars")
        if self.actions_attempted < 0:
            raise ValueError("actions_attempted must be >= 0")
        return self


@runtime_checkable
class Worker(Protocol):
    """A replaceable specialist role. implementers produce WorkerResult only."""

    name: str
    role: WorkerRole

    async def execute(self, assignment: WorkerAssignment) -> WorkerResult: ...
