"""Durable LangGraph checkpoint saver (Phase 13B, SPEC 34, 47).

Subclasses InMemorySaver to reuse its EXACT serialization semantics, then
persists the checkpoint state atomically after every mutation:

- `FileCheckpointSaver` - deterministic JSON under `<state_dir>/checkpoints/`
  (dev/local; NOT the production source of truth);
- `PostgresCheckpointSaver` - PostgreSQL-backed (SPEC 34), selected by
  `PersistenceConfig.dsn`.

Durability adds no authority: restoring a checkpoint re-enters the normal
propose -> validate -> authorize -> execute -> verify pipeline. Nothing
here creates SUCCESS, AuthorizedAction, or any permission decision.
Corrupt or version-mismatched state fails closed; checkpoints are bounded.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import ChannelVersions
from langgraph.checkpoint.memory import InMemorySaver

from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINTS_PER_THREAD = 20


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _to_jsonable(value: Any) -> Any:
    """Generic, deterministic conversion of saver internals to JSON:
    tuples -> {"__tuple__": [...]}, bytes -> {"__b64__": ...}, dicts ->
    {"__map__": [[key, value], ...]} so tuple keys survive round-trips."""
    if isinstance(value, dict):
        return {"__map__": [[_to_jsonable(k), _to_jsonable(v)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"__tuple__": [_to_jsonable(item) for item in value]}
    if isinstance(value, bytes):
        return {"__b64__": _b64(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise PersistenceCorrupt(f"unserializable checkpoint content: {type(value).__name__}")


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        if "__map__" in value:
            restored: dict[Any, Any] = {}
            for key, item in value["__map__"]:
                key = _from_jsonable(key)
                restored[tuple(key) if isinstance(key, list) else key] = _from_jsonable(item)
            return restored
        if "__tuple__" in value:
            return tuple(_from_jsonable(item) for item in value["__tuple__"])
        if "__b64__" in value:
            return _unb64(value["__b64__"])
        raise PersistenceCorrupt("malformed serialized structure")
    if isinstance(value, list):
        return [_from_jsonable(item) for item in value]
    return value


class DurableCheckpointSaver(InMemorySaver):
    """InMemorySaver + atomic persistence of the full checkpoint state
    after every mutation. Subclasses implement the snapshot transport."""

    def __init__(self, *, serde: Any = None) -> None:
        super().__init__(serde=serde)

    def _read_snapshot(self) -> dict | None:  # pragma: no cover - interface
        raise NotImplementedError

    def _write_snapshot(self, state: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def _trim(self) -> None:
        """Bound checkpoints: keep the latest MAX_CHECKPOINTS_PER_THREAD
        per (thread, namespace); prune writes and blobs no longer
        referenced by the surviving checkpoints."""
        referenced: set[tuple[str, str, Any, Any]] = set()
        for thread_id, namespaces in self.storage.items():
            for ns, cps in namespaces.items():
                latest = list(cps.keys())[-MAX_CHECKPOINTS_PER_THREAD:]
                for cp_id in list(cps.keys()):
                    if cp_id not in latest:
                        del cps[cp_id]
                        self.writes.pop((thread_id, ns, cp_id), None)
                for cp_id in cps:
                    ckpt = self.serde.loads_typed(cps[cp_id][0])
                    for channel, version in (ckpt.get("channel_versions") or {}).items():
                        referenced.add((thread_id, ns, channel, version))
        self.blobs = {key: value for key, value in self.blobs.items() if key in referenced}

    def _snapshot(self) -> dict:
        self._trim()
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "kind": "langgraph_checkpoints",
            "storage": _to_jsonable(self.storage),
            "blobs": _to_jsonable(self.blobs),
            "writes": _to_jsonable(self.writes),
        }

    def _hydrate(self) -> None:
        state = self._read_snapshot()
        if state is None:
            return
        if state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise PersistenceCorrupt(
                f"checkpoint schema version mismatch: {state.get('schema_version')!r}"
            )
        if state.get("kind") != "langgraph_checkpoints":
            raise PersistenceCorrupt("unknown checkpoint snapshot kind")
        for field in ("storage", "blobs", "writes"):
            if field not in state:
                raise PersistenceCorrupt(f"checkpoint snapshot missing {field!r}")
        storage = _from_jsonable(state["storage"])
        blobs = _from_jsonable(state["blobs"])
        writes = _from_jsonable(state["writes"])
        if isinstance(storage, dict):
            self.storage.update(storage)
        if isinstance(blobs, dict):
            self.blobs.update(blobs)
        if isinstance(writes, dict):
            self.writes.update(writes)

    def put(self, config, checkpoint, metadata, new_versions: ChannelVersions):
        result = super().put(config, checkpoint, metadata, new_versions)
        self._write_snapshot(self._snapshot())
        return result

    def put_writes(self, config, writes, task_id, task_path: str = ""):
        result = super().put_writes(config, writes, task_id, task_path=task_path)
        self._write_snapshot(self._snapshot())
        return result


class FileCheckpointSaver(DurableCheckpointSaver):
    """Deterministic JSON-backed checkpoint saver for dev/unit tests
    (SPEC 10 fake adapters): NOT the production source of truth."""

    engine_id = "checkpoint-file"

    def __init__(self, state_dir: str | Path, *, serde: Any = None) -> None:
        super().__init__(serde=serde)
        self._path = Path(state_dir) / "checkpoints" / "langgraph-threads.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._hydrate()

    def _read_snapshot(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersistenceCorrupt("corrupt checkpoint snapshot") from exc

    def _write_snapshot(self, state: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True)
            _replace_with_retry(Path(tmp), self._path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 4) -> None:
    """os.replace can transiently fail on Windows (AV/OneDrive locks);
    bounded retry keeps atomicity without masking real failures."""
    import time

    for attempt in range(3):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(0.05 * (2**attempt))
    os.replace(tmp, target)



class PostgresCheckpointSaver(DurableCheckpointSaver):
    """PostgreSQL-backed checkpoint saver (SPEC 34 durable source of
    truth). One versioned snapshot row; same fail-closed semantics."""

    engine_id = "checkpoint-postgres"

    _SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS nomadic_checkpoints (
        id             INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        state          JSONB NOT NULL,
        updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0, serde: Any = None) -> None:
        super().__init__(serde=serde)
        import psycopg

        try:
            self._conn = psycopg.connect(
                dsn, autocommit=True, connect_timeout=max(1, int(connect_timeout_s))
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"PostgreSQL unavailable: {exc}") from exc
        try:
            self._conn.execute(self._SCHEMA_SQL)
        except Exception as exc:
            raise PersistenceUnavailable(f"checkpoint schema init failed: {exc}") from exc
        self._hydrate()

    def _read_snapshot(self) -> dict | None:
        try:
            row = self._conn.execute(
                "SELECT state FROM nomadic_checkpoints WHERE id = 1"
            ).fetchone()
        except Exception as exc:
            raise PersistenceUnavailable(f"checkpoint read failed: {exc}") from exc
        if row is None:
            return None
        state = row[0]
        if not isinstance(state, dict):
            raise PersistenceCorrupt("checkpoint snapshot is not a mapping")
        return state

    def _write_snapshot(self, state: dict) -> None:
        from psycopg.types.json import Jsonb

        try:
            self._conn.execute(
                "INSERT INTO nomadic_checkpoints (id, schema_version, state) "
                "VALUES (1, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "schema_version = EXCLUDED.schema_version, "
                "state = EXCLUDED.state, updated_at = now()",
                (CHECKPOINT_SCHEMA_VERSION, Jsonb(state)),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"checkpoint persist failed: {exc}") from exc


def make_durable_saver(persistence) -> DurableCheckpointSaver:
    """Configuration-selected durable saver (Phase 13B seam): PostgreSQL
    when PersistenceConfig.dsn is set, otherwise the deterministic file
    adapter under state_dir. No hidden global state."""
    if persistence is not None and persistence.dsn:
        return PostgresCheckpointSaver(persistence.dsn)
    state_dir = getattr(persistence, "state_dir", None) or "data/state"
    return FileCheckpointSaver(state_dir)
