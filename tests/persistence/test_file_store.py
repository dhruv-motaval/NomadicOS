"""Phase 13A deterministic file-adapter tests (tests/persistence)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomadicos.contracts.core import TaskStatus
from nomadicos.persistence import (
    JsonTaskStateStore,
    PersistenceCorrupt,
    new_record,
)

TID = "task_00000000000000000001"


def test_round_trip_and_deterministic_serialization(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "Build the report")
    store.save_task(r)
    loaded = store.load_task(TID)
    assert loaded == r
    file = tmp_path / "persistence" / "tasks" / f"{TID}.json"
    first = file.read_text(encoding="utf-8")
    store.save_task(r)
    assert file.read_text(encoding="utf-8") == first, "serialization is deterministic"


def test_missing_task_returns_none(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    assert store.load_task("task_ffffffffffffffffffff") is None


def test_replace_semantics_single_task(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "objective one")
    store.save_task(r)
    r2 = r.model_copy(update={"status": TaskStatus.SUCCESS, "outcome_note": "done"})
    store.save_task(r2)
    loaded = store.load_task(TID)
    assert loaded.status is TaskStatus.SUCCESS
    assert store.task_ids() == [TID]


def test_reopen_reads_durable_state(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "objective one")
    r2 = r.model_copy(update={"status": TaskStatus.RUNNING})
    store.save_task(r2)
    store.close()
    reopened = JsonTaskStateStore(tmp_path)
    loaded = reopened.load_task(TID)
    assert loaded.status is TaskStatus.RUNNING
    assert loaded.objective == "objective one"


def test_multiple_tasks_are_isolated(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    a = new_record("task_" + "a" * 20, "task A truth")
    b = new_record("task_" + "b" * 20, "task B truth")
    store.save_task(a)
    store.save_task(b)
    assert store.load_task("task_" + "a" * 20).objective == "task A truth"
    assert store.load_task("task_" + "b" * 20).objective == "task B truth"
    assert len(store.task_ids()) == 2


def test_task_ids_limit(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    for i in range(5):
        store.save_task(new_record(f"task_{i:020d}", f"objective {i}"))
    assert len(store.task_ids(limit=3)) == 3
    assert store.task_ids() == sorted(store.task_ids())


def test_corrupt_file_fails_closed(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    file = tmp_path / "persistence" / "tasks" / f"{TID}.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("{not json", encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        store.load_task(TID)


def test_unknown_fields_fail_closed(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    store.save_task(new_record(TID, "objective"))
    file = tmp_path / "persistence" / "tasks" / f"{TID}.json"
    env = json.loads(file.read_text(encoding="utf-8"))
    env["record"]["hostile_unknown_field"] = {"authorized": True}
    file.write_text(json.dumps(env), encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        store.load_task(TID)


def test_schema_version_mismatch_fails_closed(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    store.save_task(new_record(TID, "objective here"))
    file = tmp_path / "persistence" / "tasks" / f"{TID}.json"
    env = json.loads(file.read_text(encoding="utf-8"))
    env["schema_version"] = 99
    file.write_text(json.dumps(env), encoding="utf-8")
    with pytest.raises(Exception, match="version"):
        store.load_task(TID)


def test_cross_task_contamination_rejected(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    store.save_task(new_record("task_" + "a" * 20, "task A truth"))
    a_file = tmp_path / "persistence" / "tasks" / ("task_" + "a" * 20 + ".json")
    (tmp_path / "persistence" / "tasks" / ("task_" + "b" * 20 + ".json")).write_text(
        a_file.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(PersistenceCorrupt):
        store.load_task("task_" + "b" * 20)


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    store.save_task(new_record(TID, "objective"))
    store.save_task(new_record(TID, "objective updated"))
    residue = list((tmp_path / "persistence" / "tasks").glob("*.tmp"))
    assert residue == []
    assert store.load_task(TID) is not None


def test_unsafe_task_id_never_creates_files(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    with pytest.raises(ValueError):
        store.load_task("../../escape")
    assert not list((tmp_path / "persistence").rglob("*.json"))


def test_round_trip_is_deterministic(tmp_path: Path) -> None:
    store = JsonTaskStateStore(tmp_path)
    r = new_record(TID, "objective")
    frozen = "2026-01-01T00:00:00+00:00"
    r = r.model_copy(update={"created_at": frozen, "updated_at": frozen})
    store.save_task(r)
    file = tmp_path / "persistence" / "tasks" / f"{TID}.json"
    first = file.read_bytes()
    store.save_task(r)
    assert file.read_bytes() == first
