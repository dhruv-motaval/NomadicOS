"""Canonical task/action IR — validation matrix (rebuild plan §6 tests 1-10,
12, 14-15 at the IR boundary; executor-boundary proof in tests/integration)."""
import json

import pytest

from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import ValidationError
from nomadicos.core.task_ir import (
    AUTHORITY_FIELDS,
    ActionClaim,
    ActionKind,
    TaskAction,
)


def _bind(claim: ActionClaim, task_id: str = "t-1", step_id: str = "a1-s1") -> TaskAction:
    return TaskAction.bind(
        claim,
        task_id=task_id,
        step_id=step_id,
        attempt=1,
        risk=RiskLevel.MEDIUM,
        capabilities=("filesystem.write",),
    )


# --- 1. valid canonical IR ---------------------------------------------------
def test_valid_tool_claim_and_binding() -> None:
    claim = ActionClaim.from_model_text(
        json.dumps({"tool": "filesystem", "arguments": {"action": "write"}, "finished": False})
    )
    assert claim.kind is ActionKind.TOOL_CALL
    assert claim.tool == "filesystem"
    action = _bind(claim)
    assert action.task_id == "t-1"
    assert action.risk is RiskLevel.MEDIUM
    assert action.capabilities == ("filesystem.write",)


def test_reply_and_finish_claims() -> None:
    assert ActionClaim.from_model_text('{"reply": "hi"}').kind is ActionKind.REPLY
    fin = ActionClaim.from_model_text('{"finished": true}')
    assert fin.kind is ActionKind.FINISH and fin.finish is True


# --- 2. missing required field ----------------------------------------------
def test_empty_payload_is_never_finished() -> None:
    claim = ActionClaim.from_model_text("{}")
    assert claim.kind is ActionKind.INVALID  # explicit rule §6: never "finished"
    assert claim.reason


# --- 3. wrong argument type --------------------------------------------------
def test_wrong_types_rejected() -> None:
    assert ActionClaim.from_model_text('{"tool": 123}').kind is ActionKind.INVALID
    bad_args = '{"tool": "x", "arguments": "str"}'
    assert ActionClaim.from_model_text(bad_args).kind is ActionKind.INVALID
    bad_fin = '{"reply": "hi", "finished": "yes"}'
    assert ActionClaim.from_model_text(bad_fin).kind is ActionKind.INVALID


# --- 4/5. unknown action + fake capabilities ---------------------------------
def test_tool_name_shape_enforced() -> None:
    claim = ActionClaim.from_model_text('{"tool": "../evil", "arguments": {}}')
    assert claim.kind is ActionKind.INVALID


def test_capabilities_field_is_authority_and_rejected() -> None:
    claim = ActionClaim.from_model_text(
        '{"tool": "filesystem", "arguments": {}, "capabilities": ["sudo"]}'
    )
    assert claim.kind is ActionKind.INVALID
    assert "authority" in (claim.reason or "")


# --- 6. invalid risk value (also authority) ----------------------------------
def test_risk_field_rejected() -> None:
    assert ActionClaim.from_model_text('{"tool": "x", "risk": "none"}').kind is ActionKind.INVALID


# --- 7. invalid schema_version ------------------------------------------------
def test_bad_schema_version_rejected() -> None:
    bad = '{"schema_version": "2", "tool": "x", "arguments": {}}'
    assert ActionClaim.from_model_text(bad).kind is ActionKind.INVALID


# --- 8. unexpected extra fields ----------------------------------------------
def test_unknown_extra_field_rejected() -> None:
    claim = ActionClaim.from_model_text('{"tool": "x", "arguments": {}, "mystery": 5}')
    assert claim.kind is ActionKind.INVALID


# --- 9. oversized arguments ---------------------------------------------------
def test_oversized_arguments_rejected() -> None:
    many = {f"k{i}": 1 for i in range(200)}
    claim = ActionClaim.from_model_text(
        json.dumps({"tool": "x", "arguments": many})
    )
    assert claim.kind is ActionKind.INVALID
    big = json.dumps({"tool": "x", "arguments": {"blob": "y" * 9000}})
    assert ActionClaim.from_model_text(big).kind is ActionKind.INVALID


# --- malformed model JSON + reasoning tolerance -------------------------------
def test_prose_with_embedded_json_parses_and_think_stripped() -> None:
    claim = ActionClaim.from_model_text(
        "REASON: read then write\n<tool_call>analysis about {braces}<tool_call>\n"
        '{"tool": "filesystem", "arguments": {"action": "read"}, "finished": false}'
    )
    assert claim.kind is ActionKind.TOOL_CALL
    # stray brace-containing junk before the object must confuse nothing:
    assert ActionClaim.from_model_text("no json here at all {oops").kind is ActionKind.INVALID


# --- 10. fake authorization fields --------------------------------------------
@pytest.mark.parametrize("field", sorted(AUTHORITY_FIELDS))
def test_authority_fields_are_hard_rejected(field: str) -> None:
    claim = ActionClaim.from_model_text(
        json.dumps({"tool": "filesystem", "arguments": {}, field: True})
    )
    assert claim.kind is ActionKind.INVALID
    assert field in (claim.reason or "")


# --- 12. deterministic serialization + identity ignored -----------------------
def test_serialization_deterministic_and_identity_ignored_from_model() -> None:
    a = _bind(ActionClaim.from_model_text('{"tool": "x", "arguments": {}}'))
    b = _bind(ActionClaim.from_model_text('{"tool": "x", "arguments": {}}'))
    assert a.to_json() == b.to_json()
    c = ActionClaim.from_model_text(
        '{"tool": "x", "arguments": {}, "task_id": "evil", "step_id": "evil"}'
    )
    assert c.kind is ActionKind.TOOL_CALL  # identity noise ignored, not acted on
    assert _bind(c, task_id="t-9").task_id == "t-9"


# --- 11 (IR side): invalid/empty claims can NEVER bind ------------------------
def test_invalid_claim_cannot_be_bound() -> None:
    claim = ActionClaim.from_model_text("total junk")
    with pytest.raises(ValidationError):
        _bind(claim)


# --- 15 (IR side): stable identity, retry relationship ------------------------
def test_retry_identity_is_stable_task_distinct_step() -> None:
    claim = ActionClaim.from_model_text('{"tool": "x", "arguments": {}}')
    first = _bind(claim, task_id="T", step_id="attempt-1-step-1")
    second = _bind(claim, task_id="T", step_id="attempt-2-step-1")
    assert first.task_id == second.task_id == "T"
    assert first.step_id != second.step_id
    ident = second.identity_for(
        __import__("nomadicos.security.permissions", fromlist=["SubjectIdentity"]).SubjectIdentity(
            user_id="u", session_id="s"
        )
    )
    assert ident.step_id == "attempt-2-step-1"
