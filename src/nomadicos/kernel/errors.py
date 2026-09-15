"""Structured error taxonomy — failures are explicit and typed (SPEC §3, §30).

Malformed/unknown things fail closed: code branches must never turn a failure
into an implicit success (SPEC §30, §56.14).
"""

from __future__ import annotations

from enum import StrEnum


class Failure(StrEnum):
    """Machine-readable failure categories (SPEC §30)."""

    INVALID_PROPOSAL = "INVALID_PROPOSAL"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    ACTION_FAILED = "ACTION_FAILED"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    MODEL_ERROR = "MODEL_ERROR"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    GOAL_NOT_SATISFIED = "GOAL_NOT_SATISFIED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    CONFIG_INVALID = "CONFIG_INVALID"
    REVOKED = "REVOKED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class NomadicOSBaseError(Exception):
    """Root of the error tree. Carries a category and structured context."""

    category: Failure = Failure.ACTION_FAILED

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    @property
    def failure(self) -> Failure:
        return self.category

    def to_dict(self) -> dict[str, object]:
        return {
            "error": type(self).__name__,
            "category": self.category.value,
            "message": self.message,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.category.value}): {self.message}"


class ConfigInvalid(NomadicOSBaseError):
    category = Failure.CONFIG_INVALID


class InvalidProposal(NomadicOSBaseError):
    category = Failure.INVALID_PROPOSAL


class AuthorizationDenied(NomadicOSBaseError):
    category = Failure.AUTHORIZATION_DENIED


class UnknownCapability(AuthorizationDenied):
    """Unknown capabilities fail closed (SPEC §56.14)."""


class RevokedAuthority(NomadicOSBaseError):
    category = Failure.REVOKED


class ActionFailed(NomadicOSBaseError):
    category = Failure.ACTION_FAILED


class ActionTimeout(NomadicOSBaseError):
    category = Failure.TIMEOUT


class ToolError(NomadicOSBaseError):
    category = Failure.TOOL_ERROR


class ModelError(NomadicOSBaseError):
    category = Failure.MODEL_ERROR


class EngineUnavailable(ModelError):
    category = Failure.RESOURCE_UNAVAILABLE


class VerificationFailed(NomadicOSBaseError):
    category = Failure.VERIFICATION_FAILED


class GoalNotSatisfied(NomadicOSBaseError):
    category = Failure.GOAL_NOT_SATISFIED


class ResourceUnavailable(NomadicOSBaseError):
    category = Failure.RESOURCE_UNAVAILABLE


class BudgetExhausted(NomadicOSBaseError):
    category = Failure.BUDGET_EXHAUSTED
