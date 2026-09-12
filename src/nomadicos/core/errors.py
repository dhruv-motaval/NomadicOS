"""Canonical failure taxonomy (BP §117) + domain exceptions (BP §84).

The 10-class taxonomy is the single failure vocabulary (ADR-0026/F7). Legacy
vocabularies from superseded builds map into these classes
(docs/architecture/BLUEPRINT_ADDENDUM_v0.1.md §3).
"""

from enum import StrEnum


class FailureClass(StrEnum):
    """Canonical 10-class failure taxonomy (BP §117)."""

    MODEL_FAILURE = "MODEL_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    VISION_FAILURE = "VISION_FAILURE"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    PERMISSION_FAILURE = "PERMISSION_FAILURE"
    RESOURCE_FAILURE = "RESOURCE_FAILURE"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    PLANNING_FAILURE = "PLANNING_FAILURE"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class NomadicError(Exception):
    """Base for all domain errors — carries machine-readable context (BP §84)."""

    def __init__(self, message: str, context: dict | None = None) -> None:
        super().__init__(message)
        self.context: dict = context or {}


class PermissionDenied(NomadicError):
    """Authorization refused (BP §84)."""


class SecurityPolicyViolation(NomadicError):
    """Security Gate refusal — fail closed (BP §85)."""


class ToolExecutionError(NomadicError):
    """Tool failed; failures become evidence for recovery (BP §241)."""


class ModelUnavailable(NomadicError):
    """No suitable local model available — BLOCK or ASK USER (BP §149)."""


class ModelFailure(NomadicError):
    """A local model call failed mid-flight (BP §117: MODEL_FAILURE)."""

    def __init__(self, message: str, context: dict | None = None) -> None:
        merged = {**(context or {}), "failure_class": FailureClass.MODEL_FAILURE.value}
        super().__init__(message, merged)


class ModelResourceError(NomadicError):
    """Model cannot run within current resource profile (BP §53)."""


class MemoryAccessDenied(NomadicError):
    """Memory read/write outside authorized scope (BP §60)."""


class NetworkDenied(NomadicError):
    """Network destination/policy refusal (BP §195-196, §242)."""


class ValidationError(NomadicError):
    """Input or schema validation failure."""


class StatePersistenceError(NomadicError):
    """A lifecycle state that could NOT be durably recorded aborts the
    task truthfully (STEP 4): unrecorded states are never claimed or
    continued through."""

class StateConflict(NomadicError):
    """Authoritative CAS violation on tasks.status (STEP 4): the row is not in
    the state we expected, so a race changed it first — never overwrite blindly."""


class TaskTimeout(NomadicError):
    """Task budget exhausted (BP §72)."""


class BudgetExceeded(NomadicError):
    """Any budget cap exceeded (BP §72)."""


class VerificationFailed(NomadicError):
    """OBSERVE→VERIFY determined the action did not achieve its goal (BP §366)."""


class ImprovementRejected(NomadicError):
    """Self-improvement candidate failed gates (BP §67, §243)."""


class RollbackRequired(NomadicError):
    """Regression detected — restore last known-good version (BP §45, §289)."""


__all__ = [
    "BudgetExceeded",
    "FailureClass",
    "MemoryAccessDenied",
    "ModelResourceError",
    "ModelUnavailable",
    "NetworkDenied",
    "NomadicError",
    "PermissionDenied",
    "RollbackRequired",
    "SecurityPolicyViolation",
    "TaskTimeout",
    "ToolExecutionError",
    "VerificationFailed",
    "ValidationError",
]
