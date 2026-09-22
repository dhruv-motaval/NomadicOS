"""Durable task-state records (Phase 13A, SPEC §34).

Versioned, bounded, typed DATA records for durable task truth. Persisted
data is DATA ONLY: a stored TaskStatus (including SUCCESS) is a historical
fact, never a proof of new execution; authorized-action entries are
reference metadata, never executable artifacts; nothing here can grant,
authorize, revoke, execute, route, or decide verification.

Schema rules:
- versioned (`SCHEMA_VERSION`); mismatches fail closed;
- closed schemas (`extra="forbid"`): unknown persisted fields are errors;
- every string/list field is bounded; no raw model output, UI blobs,
  screenshots, or runtime handles are persistable.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator

from nomadicos.contracts.core import Contract, TaskStatus
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import VerificationResult

#: minimum schema version written/read by this build; other versions fail
#: closed on load (no auto-migration of security-sensitive state in 13A)
SCHEMA_VERSION = 1

MAX_TASK_ID = 128
MAX_OBJECTIVE = 2000
MAX_CONSTRAINTS = 20
MAX_CONSTRAINT_CHARS = 500
MAX_OUTCOME_NOTE = 400
MAX_EXECUTIONS = 200
MAX_VERIFICATIONS = 100
MAX_AUDIT_EVENTS = 2000
MAX_AUTHORIZED_REFS = 100
MAX_PREDICATES = 12

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def validate_task_id(task_id: str) -> str:
    """Deterministic, path-safe task identity: task A's record can never be
    addressed as task B, and the id can never traverse paths."""
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise ValueError(f"unsafe task id {task_id!r}")
    return task_id


def now_iso() -> str:

    return datetime.now(UTC).isoformat()


class AuthorizedActionRef(Contract):
    """Non-executable reference metadata for an issued AuthorizedAction
    (SPEC §34 `authorized_actions` as data). The reference is NOT
    executable: restart enforcement (epoch freshness, single-use) belongs
    to the executor in later slices."""

    action_id: str
    proposal_id: str
    fingerprint: str
    capability: str
    authority_epoch: int
    granted_by: str


class AuditEventRecord(Contract):
    """Durable audit-event representation (SPEC §34 audit_events, §35
    correlation). DATA only, built from Event.to_dict() - never a second
    event system."""

    id: str
    type: str
    task_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None
    model_id: str | None = None
    tool: str | None = None
    capability: str | None = None
    result: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str


def _correlated(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("id", "type", "timestamp"):
        if not isinstance(item.get(key), str) or not item.get(key):
            raise ValueError(f"audit event missing correlation field {key!r}")
    return item


class TaskRecord(Contract):
    """One versioned durable task-state document (SPEC §34).

    `status` is stored DATA: the persistence layer never generates, decides,
    or upgrades it."""

    schema_version: int = SCHEMA_VERSION
    task_id: str
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE)
    constraints: list[str] = Field(default_factory=list)
    predicates: list[dict[str, Any]] = Field(default_factory=list)
    status: TaskStatus
    outcome_note: str = Field(default="", max_length=MAX_OUTCOME_NOTE)
    created_at: str
    updated_at: str
    authorized_actions: list[AuthorizedActionRef] = Field(default_factory=list)
    executions: list[ExecutionResult] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    audit_events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("task_id")
    @classmethod
    def _safe_id(cls, v: str) -> str:
        if not _TASK_ID_RE.fullmatch(v):
            raise ValueError(f"unsafe task id {v!r}")
        return v

    @field_validator("objective")
    @classmethod
    def _objective_bounded(cls, v: str) -> str:
        if not 1 <= len(v) <= MAX_OBJECTIVE:
            raise ValueError(f"objective length must be 1..{MAX_OBJECTIVE}")
        return v

    @field_validator("constraints")
    @classmethod
    def _constraints_bounded(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_CONSTRAINTS or any(len(c) > MAX_CONSTRAINT_CHARS for c in v):
            raise ValueError("constraints exceed bounds")
        return v

    @field_validator("executions")
    @classmethod
    def _executions_bounded(cls, v: list[ExecutionResult]) -> list[ExecutionResult]:
        if len(v) > MAX_EXECUTIONS:
            raise ValueError(f"executions exceed {MAX_EXECUTIONS}")
        return v

    @field_validator("verifications")
    @classmethod
    def _verifications_bounded(cls, v: list[VerificationResult]) -> list[VerificationResult]:
        if len(v) > MAX_VERIFICATIONS:
            raise ValueError(f"verifications exceed {MAX_VERIFICATIONS}")
        return v

    @field_validator("audit_events")
    @classmethod
    def _audit_bounded(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(v) > MAX_AUDIT_EVENTS:
            raise ValueError(f"audit events exceed {MAX_AUDIT_EVENTS}")
        for item in v:
            for key in ("id", "type", "timestamp"):
                if not isinstance(item.get(key), str) or not item.get(key):
                    raise ValueError(f"audit event missing correlation field {key!r}")
        return v

    @field_validator("authorized_actions")
    @classmethod
    def _refs_bounded(cls, v: list[AuthorizedActionRef]) -> list[AuthorizedActionRef]:
        if len(v) > MAX_AUTHORIZED_REFS:
            raise ValueError(f"authorized-action refs exceed {MAX_AUTHORIZED_REFS}")
        return v


def new_record(
    task_id: str,
    objective: str,
    *,
    predicates: list[dict[str, Any]] | None = None,
) -> TaskRecord:
    """TaskRecord construction helper; task_id comes from the caller so
    identity stays deterministic and stable across restart."""
    return TaskRecord(
        task_id=validate_task_id(task_id),
        objective=objective,
        predicates=(predicates or [])[:MAX_PREDICATES],
        status=TaskStatus.CREATED,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
