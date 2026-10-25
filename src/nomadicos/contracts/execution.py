"""Execution + observation contracts (SPEC §21, §24, §27)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from nomadicos.contracts.core import Contract
from nomadicos.kernel.errors import Failure
from nomadicos.kernel.ids import new_id


class ExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    #: tool ran, objective-level effect failed (e.g. non-zero exit) (§24)
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ERRORED = "ERRORED"  #: tool/infrastructure error before/while running


class ExecutionResult(Contract):
    """Evidence produced by the executor — attributable to a task (§23, §35)."""

    id: str = Field(default_factory=lambda: new_id("exec"))
    task_id: str
    step_id: str
    action_id: str
    action_fingerprint: str
    model_id: str
    tool: str
    operation: str
    status: ExecutionStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    #: Structured proof of what changed: paths, hashes, pids, durations.
    evidence: dict[str, Any] = Field(default_factory=dict)
    process_id: int | None = None
    failure: Failure | None = None
    message: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def succeeded(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED


class IntentStatus(StrEnum):
    """Durable execution-intent lifecycle (§56.14 crash-gap hardening).

    Recorded BEFORE the side-effecting tool runs so that, on restart, we can
    distinguish "never started" from "may have started but result lost".
    """

    INTENT_RECORDED = "INTENT_RECORDED"  # decision made, tool not yet invoked
    EXECUTION_STARTED = "EXECUTION_STARTED"  # side-effect boundary about to be crossed
    COMPLETED = "COMPLETED"  # terminal result durably recorded


class ExecutionIntent(Contract):
    """Durable record of an execution intent, recorded before side effects.

    Data only — never authority, never executable permission. The intent is
    claimed atomically (first-writer-wins) so concurrent workers or resumed
    processes cannot both initiate the same operation.
    """

    status: IntentStatus = IntentStatus.INTENT_RECORDED
    task_id: str
    step_id: str
    fingerprint: str
    authorization_epoch: int
    claimed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    tool: str
    operation: str
    args: dict[str, Any] = Field(default_factory=dict)


class ObservationKind(StrEnum):
    TERMINAL = "TERMINAL"
    FILESYSTEM = "FILESYSTEM"
    PROCESS = "PROCESS"
    DESKTOP = "DESKTOP"
    STEP_RESULT = "STEP_RESULT"
    NOTE = "NOTE"


class Observation(Contract):
    """Observed world state fed back to workers. Data, never authority (§7)."""

    id: str = Field(default_factory=lambda: new_id("obs"))
    task_id: str
    kind: ObservationKind
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
