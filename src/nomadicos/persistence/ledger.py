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
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nomadicos.contracts.execution import ExecutionIntent, ExecutionResult, IntentStatus
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
    first writer wins; a record is never silently replaced.

    Execution intents are stored alongside results in the same document,
    keyed by the same action key, so a crash gap between intent and result
    is still durable on restart.
    """

    engine_id = "ledger-file"

    def __init__(self, state_dir: str | Path) -> None:
        self._path = Path(state_dir) / "persistence" / "execution-ledger.json"
        self.__lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: dict[str, dict[str, Any]] = {}
        self._intents: dict[str, dict[str, Any]] = {}
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
        intents = raw.get("intents")
        if intents is not None and isinstance(intents, dict):
            self._intents = intents

    def _rehydrate_from_disk(self) -> None:
        """Re-read the ledger file from disk to pick up writes by other
        processes. Called under the lock in claim_intent."""
        if not self._path.exists():
            self._entries = {}
            self._intents = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return  # corrupt file: keep in-memory state rather than crash
        entries = raw.get("entries")
        intents = raw.get("intents")
        if isinstance(entries, dict):
            self._entries = entries
        if isinstance(intents, dict):
            self._intents = intents

    def _acquire_lock(self, timeout: float = 10.0) -> int | None:
        """Acquire an exclusive cross-process lock via O_CREAT | O_EXCL.

        Returns the file descriptor if acquired, None on timeout.
        The lock file is deleted on release. Stale lock files (from a
        crashed process) are not automatically reaped; the timeout bounds
        how long a caller waits and the claim then fails CLOSED (refused).

        On Windows a sharing violation surfaces as PermissionError (EACCES)
        rather than FileExistsError when another process holds the lock
        file open, so BOTH mean "lock busy - retry".
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                fd = os.open(
                    str(self.__lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
                return fd
            except FileExistsError:
                time.sleep(0.01)
            except PermissionError:
                # Windows sharing violation while another claimant holds the
                # lock open: same meaning as FileExistsError (SPEC §56.14).
                time.sleep(0.01)
        return None

    def _release_lock(self, fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(str(self.__lock_path))
        except OSError:
            pass

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

    def claim_intent(self, key: str, intent: ExecutionIntent) -> bool:
        """Atomically claim execution intent: first-writer-wins.

        Returns True if this caller won the claim; False if another worker or
        resumed process already claimed it (concurrency safety, §56.14).

        Cross-process safe: holds an exclusive lock file while re-hydrating
        from disk and checking/writing the intent, so two independent
        processes cannot both claim the same key.
        """
        _validate_key(key)
        lock_fd = self._acquire_lock()
        try:
            if lock_fd is None:
                # Could not acquire lock in time: conservatively refuse the
                # claim to avoid double execution.
                return False
            # Re-hydrate from disk to see writes by other processes
            self._rehydrate_from_disk()
            if key in self._intents:
                return False
            self._intents[key] = intent.model_dump(mode="json")
            self._flush()
            return True
        finally:
            self._release_lock(lock_fd)

    def get_intent(self, key: str) -> ExecutionIntent | None:
        _validate_key(key)
        entry = self._intents.get(key)
        if entry is None:
            return None
        try:
            return ExecutionIntent.model_validate(entry)
        except Exception as exc:
            raise PersistenceCorrupt(f"corrupt intent entry {key!r}") from exc

    def mark_intent_started(self, key: str) -> None:
        _validate_key(key)
        entry = self._intents.get(key)
        if entry is not None:
            entry["status"] = IntentStatus.EXECUTION_STARTED.value
            self._flush()

    def mark_intent_completed(self, key: str) -> None:
        _validate_key(key)
        entry = self._intents.get(key)
        if entry is not None:
            entry["status"] = IntentStatus.COMPLETED.value
            entry["completed_at"] = datetime.now(UTC).isoformat()
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
                        "intents": self._intents,
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

    _INTENT_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS nomadic_execution_intents (
        action_key   TEXT PRIMARY KEY,
        task_id      TEXT NOT NULL,
        step_id      TEXT NOT NULL,
        fingerprint  TEXT NOT NULL,
        authority_epoch INTEGER NOT NULL,
        status       TEXT NOT NULL,
        claimed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        tool         TEXT NOT NULL,
        operation    TEXT NOT NULL,
        args         JSONB NOT NULL DEFAULT '{}'
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
            self._conn.execute(self._INTENT_SCHEMA_SQL)
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

    def claim_intent(self, key: str, intent: ExecutionIntent) -> bool:
        """Atomically claim execution intent via INSERT ... ON CONFLICT DO
        NOTHING. Returns True if this caller won the claim."""
        from psycopg.types.json import Jsonb

        _validate_key(key)
        try:
            row = self._conn.execute(
                """
                INSERT INTO nomadic_execution_intents
                    (action_key, task_id, step_id, fingerprint, authority_epoch,
                     status, tool, operation, args)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (action_key) DO NOTHING
                RETURNING action_key
                """,
                (
                    key,
                    intent.task_id,
                    intent.step_id,
                    intent.fingerprint,
                    intent.authorization_epoch,
                    intent.status.value,
                    intent.tool,
                    intent.operation,
                    Jsonb(intent.args),
                ),
            ).fetchone()
            return row is not None
        except Exception as exc:
            raise PersistenceUnavailable(f"intent claim failed: {exc}") from exc

    def get_intent(self, key: str) -> ExecutionIntent | None:
        _validate_key(key)
        try:
            row = self._conn.execute(
                "SELECT task_id, step_id, fingerprint, authority_epoch, "
                "status, tool, operation, args "
                "FROM nomadic_execution_intents WHERE action_key = %s",
                (key,),
            ).fetchone()
        except Exception as exc:
            raise PersistenceUnavailable(f"intent read failed: {exc}") from exc
        if row is None:
            return None
        try:
            return ExecutionIntent(
                status=IntentStatus(row[4]),
                task_id=row[0],
                step_id=row[1],
                fingerprint=row[2],
                authorization_epoch=row[3],
                tool=row[5],
                operation=row[6],
                args=row[7] or {},
            )
        except Exception as exc:
            raise PersistenceCorrupt(f"corrupt intent entry {key!r}") from exc

    def mark_intent_started(self, key: str) -> None:
        _validate_key(key)
        try:
            self._conn.execute(
                "UPDATE nomadic_execution_intents SET status = %s "
                "WHERE action_key = %s AND status = %s",
                (IntentStatus.EXECUTION_STARTED.value, key, IntentStatus.INTENT_RECORDED.value),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"intent mark-started failed: {exc}") from exc

    def mark_intent_completed(self, key: str) -> None:
        _validate_key(key)
        try:
            self._conn.execute(
                "UPDATE nomadic_execution_intents SET status = %s, "
                "completed_at = now() WHERE action_key = %s",
                (IntentStatus.COMPLETED.value, key),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"intent mark-completed failed: {exc}") from exc


def make_execution_ledger(persistence):
    """Configuration-selected durable ledger (Phase 13D): PostgreSQL when
    PersistenceConfig.dsn is set, otherwise the deterministic file adapter
    under state_dir. No hidden global state."""
    if persistence is not None and persistence.dsn:
        return PostgresExecutionLedger(persistence.dsn)
    state_dir = getattr(persistence, "state_dir", None) or "data/state"
    return DurableFileLedger(state_dir)
