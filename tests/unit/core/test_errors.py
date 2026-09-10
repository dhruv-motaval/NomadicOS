import pytest

from nomadicos.core.errors import (
    BudgetExceeded,
    FailureClass,
    NomadicError,
    PermissionDenied,
    SecurityPolicyViolation,
    VerificationFailed,
)


def test_all_blueprint_84_exceptions_exist() -> None:
    from nomadicos.core import errors

    expected = {
        "PermissionDenied",
        "SecurityPolicyViolation",
        "ToolExecutionError",
        "ModelUnavailable",
        "ModelResourceError",
        "MemoryAccessDenied",
        "NetworkDenied",
        "ValidationError",
        "TaskTimeout",
        "BudgetExceeded",
        "VerificationFailed",
        "ImprovementRejected",
        "RollbackRequired",
    }
    for name in expected:
        cls = getattr(errors, name)
        assert issubclass(cls, NomadicError)


def test_errors_carry_machine_readable_context() -> None:
    err = PermissionDenied("not allowed", context={"tool": "filesystem.delete"})
    assert err.context == {"tool": "filesystem.delete"}
    assert "not allowed" in str(err)


def test_failure_taxonomy_has_exactly_ten_classes() -> None:
    assert len(FailureClass) == 10
    assert {c.value for c in FailureClass} == {
        "MODEL_FAILURE",
        "TOOL_FAILURE",
        "VISION_FAILURE",
        "NETWORK_FAILURE",
        "PERMISSION_FAILURE",
        "RESOURCE_FAILURE",
        "ENVIRONMENT_FAILURE",
        "PLANNING_FAILURE",
        "VERIFICATION_FAILURE",
        "UNKNOWN_FAILURE",
    }


def test_budget_exceeded_is_nomadic_error() -> None:
    assert issubclass(BudgetExceeded, NomadicError)
    with pytest.raises(NomadicError):
        raise VerificationFailed("verify failed")
    with pytest.raises(NomadicError):
        raise SecurityPolicyViolation("blocked")
