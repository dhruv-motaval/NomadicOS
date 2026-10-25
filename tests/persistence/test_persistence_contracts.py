"""Phase 13A durable-record contract tests: versioning, bounds, closed
schemas, task identity, status-as-data."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.persistence import JsonTaskStateStore
from nomadicos.persistence.contracts import (
    MAX_EXECUTIONS,
    SCHEMA_VERSION,
    TaskRecord,
    new_record,
    validate_task_id,
)


def _exec(task_id: str) -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        step_id="s",
        action_id="a",
        action_fingerprint="f",
        model_id="m",
        tool="filesystem",
        operation="read",
        status="SUCCEEDED",
    )


def test_record_round_trip_defaults() -> None:
    r = new_record("task_abc123def456", "Build the report")
    assert r.schema_version == SCHEMA_VERSION
    assert r.status is TaskStatus.CREATED
    assert r.task_id == "task_abc123def456"
    restored = TaskRecord.model_validate(r.model_dump(mode="json"))
    assert restored == r


def test_task_id_must_be_safe_and_bounded() -> None:
    assert validate_task_id("task_abc123XYZ") == "task_abc123XYZ"
    for bad in ("../evil", "task with space", "", "x" * 129, "slash/inside", "dot.dot"):
        try:
            validate_task_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"unsafe task id accepted: {bad!r}")


def test_unsafe_id_is_rejected_before_any_path_is_touched(tmp_path) -> None:
    store = JsonTaskStateStore(tmp_path)
    with pytest.raises((ValueError, Exception)):
        store.save_task(
            TaskRecord(
                task_id="../escape",
                objective="x",
                status="RUNNING",
                created_at="t",
                updated_at="t",
            )
        )
    assert not list((tmp_path / "persistence").rglob("*.json"))


def test_unknown_persisted_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(
            task_id="task_ok1",
            objective="x",
            status="RUNNING",
            created_at="t",
            updated_at="t",
            authorized_by_model=True,
        )


def test_missing_required_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate({"task_id": "task_ok1", "status": "RUNNING"})
    with pytest.raises(ValidationError):
        TaskRecord.model_validate({"task_id": "task_ok1", "objective": "x", "created_at": "t"})


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskRecord(
            task_id="task_ok2",
            objective="x",
            status="GRANTED_BY_MODEL",
            created_at="t",
            updated_at="t",
        )


def test_success_round_trips_as_data() -> None:
    r = TaskRecord(
        task_id="task_ok1", objective="x", status="SUCCESS", created_at="t", updated_at="t"
    )
    assert r.status is TaskStatus.SUCCESS
    restored = TaskRecord.model_validate(r.model_dump(mode="json"))
    assert restored.status is TaskStatus.SUCCESS


def test_bounded_objective_constraints_outcome() -> None:
    with pytest.raises(ValidationError):
        new_record("task_ok1", "x" * 2001)
    r = new_record("task_ok1", "x")
    with pytest.raises(ValidationError):
        TaskRecord.model_validate({**r.model_dump(mode="json"), "constraints": ["c" * 501]})
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(
            {**r.model_dump(mode="json"), "constraints": [str(i) for i in range(21)]}
        )
    with pytest.raises(ValidationError):
        TaskRecord.model_validate({**r.model_dump(mode="json"), "outcome_note": "n" * 401})


def test_execution_list_capped() -> None:
    r = new_record("task_ok1", "objective")
    data = r.model_dump(mode="json")
    data["executions"] = [_exec("task_ok1").model_dump(mode="json")] * (MAX_EXECUTIONS + 1)
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(data)


def test_execution_round_trip() -> None:
    r = new_record("task_ok1", "objective")
    r2 = r.model_copy(update={"executions": [_exec("task_ok1")]})
    restored = TaskRecord.model_validate(r2.model_dump(mode="json"))
    assert restored.executions[0].tool == "filesystem"
    assert restored.executions[0].succeeded


def test_schema_version_is_explicit() -> None:

    assert SCHEMA_VERSION >= 1
    assert new_record("task_ok1", "x").schema_version == SCHEMA_VERSION


def test_cross_task_contamination_is_impossible() -> None:
    validate_task_id("task_11111111111111111111")
    with pytest.raises(ValueError):
        validate_task_id("../../task_22222222222222222222")
    with pytest.raises(ValueError):
        validate_task_id("")
