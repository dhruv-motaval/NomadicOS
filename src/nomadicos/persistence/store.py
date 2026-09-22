"""Durable task-state store (Phase 13A, SPEC §34).

One narrow typed interface with two implementations:

- `PostgresTaskStateStore` — PostgreSQL (SPEC §34: the durable source of
  truth for task state). Used when `PersistenceConfig.dsn` is configured.
- `JsonTaskStateStore` — deterministic single-file-per-task adapter for
  unit tests and local development. It implements the SAME interface with
  identical semantics (atomic writes, version checks, fail-closed loads)
  but is NOT the production durable source of truth.

Both adapters store versioned, bounded, typed records (see contracts.py).
Neither layer decides success, mints authority, executes tools, or routes:
persisted data is DATA.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol

from nomadicos.persistence.contracts import SCHEMA_VERSION, TaskRecord, validate_task_id
from nomadicos.persistence.errors import PersistenceCorrupt


class TaskStateStore(Protocol):
    """Durable task-state store (SPEC §34). DATA boundary only."""

    def save_task(self, record: TaskRecord) -> None: ...

    def load_task(self, task_id: str) -> TaskRecord | None: ...

    def task_ids(self, limit: int = 100) -> list[str]: ...

    def close(self) -> None: ...


def envelope_of(record: TaskRecord) -> dict[str, Any]:
    """Versioned, deterministic serialization envelope."""
    return {
        "schema_version": record.schema_version,
        "kind": "task_state",
        "record": record.model_dump(mode="json"),
    }


def decode_envelope(task_id: str, raw: Any) -> TaskRecord:
    """Fail-closed decode shared by both adapters: kind, version, shape,
    identity, and schema are re-validated on load — persisted data is
    never trusted as-is."""
    if not isinstance(raw, dict):
        raise PersistenceCorrupt(f"task record {task_id!r} is not a mapping")
    if raw.get("kind") != "task_state":
        raise PersistenceCorrupt(f"unknown record kind for task {task_id!r}")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise PersistenceCorrupt(
            f"schema version mismatch: stored {raw.get('schema_version')!r}, "
            f"this build reads {SCHEMA_VERSION}"
        )
    payload = raw.get("record")
    if not isinstance(payload, dict):
        raise PersistenceCorrupt(f"missing record body for task {task_id!r}")
    try:
        record = TaskRecord.model_validate(payload)
    except Exception as exc:
        raise PersistenceCorrupt(f"task record {task_id!r} failed validation: {exc}") from exc
    if record.task_id != task_id:
        raise PersistenceCorrupt(
            f"cross-task contamination: stored {record.task_id!r} under {task_id!r}"
        )
    return record


class JsonTaskStateStore:
    """Deterministic file adapter for unit tests and local development.

    One versioned JSON document per task under
    `<state_dir>/persistence/tasks/<task_id>.json`, written atomically
    (tempfile + os.replace, AuthorityStore pattern). Same interface and
    semantics as the PostgreSQL adapter; production durable truth remains
    PostgreSQL per SPEC §34."""

    def __init__(self, state_dir: str | Path) -> None:
        self._root = Path(state_dir) / "persistence" / "tasks"
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        validate_task_id(task_id)
        return self._root / f"{task_id}.json"

    def save_task(self, record: TaskRecord) -> None:
        path = self._path(record.task_id)
        fd, tmp = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(envelope_of(record), fh, sort_keys=True)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load_task(self, task_id: str) -> TaskRecord | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersistenceCorrupt(f"corrupt task record for {task_id!r}") from exc
        record = decode_envelope(task_id, raw)
        if record.task_id != task_id:
            raise PersistenceCorrupt(
                f"cross-task contamination: file {task_id!r} holds {record.task_id!r}"
            )
        return record

    def task_ids(self, limit: int = 100) -> list[str]:
        return sorted(p.stem for p in self._root.glob("task_*.json"))[: max(1, int(limit))]

    def close(self) -> None:
        return None
