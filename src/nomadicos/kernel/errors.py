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
    OUTCOME_UNCERTAIN = "OUTCOME_UNCERTAIN"
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


class ToolError(NomadicOSBaseError):
    category = Failure.TOOL_ERROR


class ModelError(NomadicOSBaseError):
    category = Failure.MODEL_ERROR


class EngineUnavailable(ModelError):
    category = Failure.RESOURCE_UNAVAILABLE


class ResourceUnavailable(NomadicOSBaseError):
    category = Failure.RESOURCE_UNAVAILABLE


class BudgetExhausted(NomadicOSBaseError):
    category = Failure.BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# Cloud inference failures (SPEC §10): every outcome is a typed error with a
# machine-readable ``kind``; none is ever turned into an implicit success.
# ---------------------------------------------------------------------------


class CloudInferenceError(ModelError):
    """Root of typed cloud-inference failures. ``kind`` is stable and logged;
    ``context['retryable']`` tells the engine whether retrying the SAME model
    can help."""

    kind = "cloud_error"

    @property
    def retryable(self) -> bool:
        return bool(self.context.get("retryable", True))


class ModelUnavailable(CloudInferenceError, EngineUnavailable):
    """Provider/model not reachable, not found, unauthorized, or not served."""

    kind = "unavailable"


class RateLimited(ModelUnavailable):
    """Rate limit or exhausted free quota. Never evidence that a model is paid."""

    kind = "rate_limit"


class InferenceTimeout(CloudInferenceError):
    category = Failure.TIMEOUT
    kind = "timeout"


class MalformedResponse(CloudInferenceError):
    kind = "malformed_response"


class SchemaValidationFailed(CloudInferenceError):
    kind = "schema_validation"


class ContextOverflow(CloudInferenceError):
    kind = "context_overflow"

    @property
    def retryable(self) -> bool:  # the same prompt cannot fit the same model
        return False


class RetryBudgetExhausted(CloudInferenceError):
    category = Failure.BUDGET_EXHAUSTED
    kind = "budget_exhausted"


class FreeOnlyExcluded(CloudInferenceError):
    """No owner-allowed model satisfies the request under ``free_only``."""

    category = Failure.RESOURCE_UNAVAILABLE
    kind = "free_only_excluded"
