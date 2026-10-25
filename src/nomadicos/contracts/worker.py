"""Shared worker type contracts (Phase 4B, SPEC §9).

Placed in ``nomadicos.contracts`` so both the cognitive layer (brain) and
the orchestration layer can import them without violating the brain import
boundary (brain modules may import only brain, contracts, and kernel).
"""

from __future__ import annotations

from enum import StrEnum


class WorkerRole(StrEnum):
    """Specialist roles the Brain may delegate to a mission task."""

    EXECUTE = "execute"  # direct tool action (Phase 4A default)
    CODING = "coding"  # bounded code changes via the execution harness
    RESEARCH = "research"  # information gathering with source references
    TESTING = "testing"  # acceptance criteria + test evaluation


class WorkerStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class WorkerLifecycle(StrEnum):
    """Enforceable worker lifecycle (Phase 4C, SPEC §30). Owned by trusted
    runtime code only — a worker can never set its own lifecycle state.

    CREATED -> RUNNING -> (COMPLETED | FAILED | TIMED_OUT | CANCELLED |
    OUTCOME_UNKNOWN). CANCEL_REQUESTED is the intermediate state between
    "cancel was requested" and "cleanup finished". OUTCOME_UNKNOWN is
    terminal-conservative: the worker could not be provably stopped, so its
    side effects (if any) are uncertain and it must never be auto-replayed.
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
