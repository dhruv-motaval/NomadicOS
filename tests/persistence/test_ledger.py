"""Phase 13D durable-ledger adapter tests: write/read, fresh-instance
restore, immutability, corruption fail-closed, bounded capacity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomadicos.contracts.execution import ExecutionResult
from nomadicos.persistence import PersistenceCorrupt
from nomadicos.persistence.ledger import LEDGER_SCHEMA_VERSION, DurableFileLedger

KEY = "a" * 32


def _result() -> ExecutionResult:
    return ExecutionResult(
        task_id="task_00000000000000000001",
        step_id="s1",
        action_id="act_1",
        action_fingerprint="fp",
        model_id="m",
        tool="desktop",
        operation="mouse_click",
        status="SUCCEEDED",
        evidence={"x": 1, "y": 2},
    )


def test_write_read_round_trip(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    assert ledger.get(KEY) is None
    ledger.put(KEY, _result())
    loaded = ledger.get(KEY)
    assert loaded is not None and loaded.status.value == "SUCCEEDED"
    assert loaded.evidence["x"] == 1


def test_fresh_instance_restores_durable_entries(tmp_path: Path) -> None:
    DurableFileLedger(tmp_path).put(KEY, _result())
    reopened = DurableFileLedger(tmp_path)
    loaded = reopened.get(KEY)
    assert loaded is not None and loaded.task_id == "task_00000000000000000001"


def test_entries_are_immutable_first_writer_wins(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    ledger.put(KEY, _result())
    other = _result().model_copy(update={"status": "FAILED", "message": "different"})
    ledger.put(KEY, other)
    loaded = ledger.get(KEY)
    assert loaded.status.value == "SUCCEEDED"


def test_malformed_key_rejected(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    for bad in ("", "short", "z" * 32, "a" * 33):
        with pytest.raises(PersistenceCorrupt):
            ledger.get(bad)
        with pytest.raises(PersistenceCorrupt):
            ledger.put(bad, _result())
    assert not list((tmp_path / "persistence").rglob("*.json"))


def test_corrupt_file_fails_closed(tmp_path: Path) -> None:
    DurableFileLedger(tmp_path)
    (tmp_path / "persistence" / "execution-ledger.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        DurableFileLedger(tmp_path)


def test_corrupt_entry_never_means_not_executed(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    ledger.put(KEY, _result())
    path = tmp_path / "persistence" / "execution-ledger.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["entries"][KEY] = {"broken": True}
    path.write_text(json.dumps(raw), encoding="utf-8")
    reopened = DurableFileLedger(tmp_path)
    with pytest.raises(PersistenceCorrupt):
        reopened.get(KEY)


def test_version_kind_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "persistence" / "execution-ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 99, "kind": "execution_ledger", "entries": {}}),
        encoding="utf-8",
    )
    with pytest.raises(PersistenceCorrupt):
        DurableFileLedger(tmp_path)


def test_deterministic_serialization(tmp_path: Path) -> None:
    ledger = DurableFileLedger(tmp_path)
    ledger.put(KEY, _result())
    path = tmp_path / "persistence" / "execution-ledger.json"
    first = path.read_bytes()
    DurableFileLedger(tmp_path)
    assert path.read_bytes() == first


def test_schema_version_is_explicit() -> None:
    assert LEDGER_SCHEMA_VERSION >= 1
