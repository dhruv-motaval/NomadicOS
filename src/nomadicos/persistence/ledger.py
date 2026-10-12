"""Durable execution ledger (Phase 13D, SPEC 34).

Single-use execution facts, durable across process restarts. Implements the
existing ExecutionLedger protocol (get/put - see executor/dispatch.py) with
two transports:

- `PostgresExecutionLedger` - PostgreSQL (SPEC 34 production path), keyed by
  the executor's action key; INSERT ... ON CONFLICT DO NOTHING makes
  recording transactionally first-writer-wins (race-safe);
- `DurableFileLedger` - deterministic file adapter for unit tests/dev;
  NOT the production source of truth.

The ledger is DATA: it only answers "has this action identity already been
recorded?". It never authorizes, grants, revokes, answers owner conflicts,
creates authority, writes SUCCESS, executes tools, or routes. Corrupt
entries fail closed - a malformed record is never treated as "not executed".
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from nomadicos.contracts.execution import ExecutionResult
from nomadicos.persistence.errors import (
    PersistenceCorrupt,
    PersistenceError,
    PersistenceUnavailable,
)

LEDGER_SCHEMA_VERSION = 1
MAX_LEDGER_ENTRIES = 10_000

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise PersistenceCorrupt(f"malformed ledger key {key!r}")
    return key


class DurableFileLedger:
    """Deterministic file-backed ledger (tests/dev; NOT production truth).

    One versioned JSON document under
    `<state_dir>/persistence/execution-ledger.json`, written atomically
    (tempfile + os.replace). Entries are immutable execution facts:
    first writer wins; a record is never silently replaced."""

    engine_id = "ledger-file"

    def __init__(self, state_dir: str | Path) -> None:
        self._path = Path(state_dir) / "persistence" / "execution-ledger.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict[str, Any]] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PersistenceCorrupt("corrupt execution ledger") from exc
        if (
            raw.get("schema_version") != LEDGER_SCHEMA_VERSION
            or raw.get("kind") != "execution_ledger"
        ):
            raise PersistenceCorrupt("execution ledger version/kind mismatch")
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raise PersistenceCorrupt("execution ledger entries malformed")
        self._entries = entries

    def get(self, key: str) -> ExecutionResult | None:
        _validate_key(key)
        entry = self._entries.get(key)
        if entry is None:
            return None
        try:
            return ExecutionResult.model_validate(entry)
        except Exception as exc:
            raise PersistenceCorrupt(f"corrupt ledger entry {key!r}") from exc

    def put(self, key: str, result: ExecutionResult) -> None:
        _validate_key(key)
        if key in self._entries:
            return  # immutable execution fact: first writer wins
        if len(self._entries) >= MAX_LEDGER_ENTRIES:
            raise PersistenceError(
                "execution ledger at capacity; refusing new entries (replay safety)"
            )
        self._entries[key] = result.model_dump(mode="json")
        self._flush()

    def _flush(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "schema_version": LEDGER_SCHEMA_VERSION,
                        "kind": "execution_ledger",
                        "entries": self._entries,
                    },
                    fh,
                    sort_keys=True,
                )
            for attempt in range(3):
                try:
                    os.replace(tmp, self._path)
                    return
                except PermissionError:
                    import time

                    time.sleep(0.05 * (2**attempt))
            os.replace(tmp, self._path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise


class PostgresExecutionLedger:
    """PostgreSQL-backed ledger (SPEC 34 production path). Same protocol;
    INSERT ... ON CONFLICT DO NOTHING = transactional first-writer-wins."""

    engine_id = "ledger-postgres"

    _SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS nomadic_execution_ledger (
        action_key  TEXT PRIMARY KEY,
        task_id     TEXT NOT NULL,
        result      JSONB NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
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
            raise PersistenceUnavailable(f"ledger schema init failed: {exc}") from exc

    def get(self, key: str) -> ExecutionResult | None:
        _validate_key(key)
        try:
            row = self._conn.execute(
                "SELECT result FROM nomadic_execution_ledger WHERE action_key = %s",
                (key,),
            ).fetchone()
        except Exception as exc:
            raise PersistenceUnavailable(f"ledger read failed: {exc}") from exc
        if row is None:
            return None
        try:
            return ExecutionResult.model_validate(row[0])
        except Exception as exc:
            raise PersistenceCorrupt(f"corrupt ledger entry {key!r}") from exc

    def put(self, key: str, result: ExecutionResult) -> None:
        from psycopg.types.json import Jsonb

        _validate_key(key)
        try:
            self._conn.execute(
                "INSERT INTO nomadic_execution_ledger (action_key, task_id, result) "
                "VALUES (%s, %s, %s) ON CONFLICT (action_key) DO NOTHING",
                (key, result.task_id, Jsonb(result.model_dump(mode="json"))),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"ledger write failed: {exc}") from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            return None


def make_execution_ledger(persistence):
    """Configuration-selected durable ledger (Phase 13D): PostgreSQL when
    PersistenceConfig.dsn is set, otherwise the deterministic file adapter
    under state_dir. No hidden global state."""
    if persistence is not None and persistence.dsn:
        return PostgresExecutionLedger(persistence.dsn)
    state_dir = getattr(persistence, "state_dir", None) or "data/state"
    return DurableFileLedger(state_dir)
