"""Worker boundary (SPEC §9.25, §9.29).

A worker is a replaceable strategy with a NAME and a typed REPORT.
The report deliberately cannot carry authority: the model schema is closed
(extra="forbid") and forbidden field names cannot round-trip through it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from nomadicos.contracts.core import Contract


@runtime_checkable
class Worker(Protocol):
    name: str
    kind: str

    def report_fields(self) -> list[str]: ...


class WorkerReport(Contract):
    """Structured worker accounting (SPEC §9.29/§9.30). No authority fields."""

    worker: str
    task_id: str
    model_used: str | None = None
    files_read: list[str] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    tests_run: int = 0
    test_exits: list[int | None] = Field(default_factory=list)
    repairs_used: int = 0
    actions_attempted: int = 0
    elapsed_s: float = 0.0
    verification_outcome: str | None = None
