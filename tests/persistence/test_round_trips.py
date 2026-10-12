"""Phase 13A behavioral tests: SUCCESS/AuthorizedAction/audit persist as
inert DATA through the same narrow store interface."""

from __future__ import annotations

from pathlib import Path

import pytest

from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import (
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.kernel.events import Event, EventType
from nomadicos.persistence import (
    AuthorizedActionRef,
    JsonTaskStateStore,
    TaskRecord,
    new_record,
)

TID = "task_00000000000000000001"


def test_execution_and_verification_round_trip(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "objective")
    execution = ExecutionResult(
        task_id=TID,
        step_id="s1",
        action_id="act_1",
        action_fingerprint="fp",
        model_id="m",
        tool="desktop",
        operation="mouse_click",
        status="SUCCEEDED",
        evidence={"x": 1, "y": 2, "screen": [800, 600]},
    )
    verification = VerificationResult(
        level=VerificationLevel.GOAL,
        task_id=TID,
        outcome=VerificationOutcome.PASS,
        verifier="predicate-goal-verifier",
        evidence=[{"claim": "window present: notepad", "observed": True, "detail": {}}],
    )
    r2 = r.model_copy(update={"executions": [execution], "verifications": [verification]})
    store.save_task(r2)
    loaded = store.load_task(TID)
    assert loaded.executions[0].evidence["x"] == 1
    assert loaded.verifications[0].outcome is VerificationOutcome.PASS


def test_authorized_action_stored_as_reference_not_executable(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "objective")
    r2 = r.model_copy(
        update={
            "authorized_actions": [
                AuthorizedActionRef(
                    action_id="act_1",
                    proposal_id="prop_1",
                    fingerprint="fp",
                    capability="desktop.mouse_click",
                    authority_epoch=3,
                    granted_by="policy",
                )
            ]
        }
    )
    store.save_task(r2)
    loaded = store.load_task(TID)
    ref = loaded.authorized_actions[0]
    # metadata only: identity + authorization facts, no executable surface
    assert ref.capability == "desktop.mouse_click" and ref.authority_epoch == 3
    assert not hasattr(ref, "execute") and not callable(ref)


def test_audit_event_representation_round_trip(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    event = Event(
        EventType.TOOL_EXECUTED,
        task_id=TID,
        step_id="s1",
        attempt=1,
        model_id="m",
        tool="desktop",
        capability="desktop.mouse_click",
        result="SUCCEEDED",
        payload={"note": "click done"},
    )
    r = new_record(TID, "objective")
    r2 = r.model_copy(update={"audit_events": [event.to_dict()]})
    store.save_task(r2)
    loaded = store.load_task(TID)
    persisted = loaded.audit_events[0]
    # §35 correlation fields survive the durable round-trip
    assert persisted["type"] == "TOOL_EXECUTED"
    assert persisted["task_id"] == TID
    assert persisted["attempt"] == 1
    assert persisted["capability"] == "desktop.mouse_click"


def test_audit_events_require_correlation_fields(tmp_path: Path) -> None:
    r = new_record(TID, "objective")
    data = r.model_dump(mode="json")
    data["audit_events"] = [{"id": "1", "timestamp": "t"}]
    with pytest.raises(Exception, match="correlation"):
        TaskRecord.model_validate(data)
