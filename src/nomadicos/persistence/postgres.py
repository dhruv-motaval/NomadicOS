"""PostgreSQL durable task-state store (Phase 13A, SPEC §34).

PostgreSQL is the durable source of truth for task state. This adapter
implements the same narrow TaskStateStore interface as the deterministic
file adapter, with identical fail-closed semantics: versioned envelopes,
closed schemas, identity checks, structured errors.

Connection configuration comes from PersistenceConfig.dsn (no credentials
in code). Writes use normal transaction semantics; an unreachable database
raises structured PersistenceUnavailable - never silent data loss.
"""

from __future__ import annotations

from nomadicos.persistence.contracts import TaskRecord, validate_task_id
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable
from nomadicos.persistence.store import decode_envelope, envelope_of

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nomadic_task_state (
    task_id        TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    record         JSONB NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_SAVE_SQL = """
INSERT INTO nomadic_task_state (task_id, schema_version, record)
VALUES (%s, %s, %s)
ON CONFLICT (task_id)
DO UPDATE SET schema_version = EXCLUDED.schema_version,
              record = EXCLUDED.record,
              updated_at = now()
"""


class PostgresTaskStateStore:
    """PostgreSQL adapter: same interface and fail-closed semantics as the
    deterministic file adapter (SPEC §34 durable source of truth)."""

    def __init__(self, dsn: str, *, connect_timeout_s: float = 5.0) -> None:
        import psycopg

        self._psycopg = psycopg
        try:
            self._conn = psycopg.connect(
                dsn, autocommit=True, connect_timeout=max(1, int(connect_timeout_s))
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"PostgreSQL unavailable: {exc}") from exc
        try:
            self._conn.execute(_SCHEMA_SQL)
        except Exception as exc:
            raise PersistenceUnavailable(f"schema initialization failed: {exc}") from exc

    def save_task(self, record: TaskRecord) -> None:
        from psycopg.types.json import Jsonb

        try:
            self._conn.execute(
                _SAVE_SQL,
                (record.task_id, record.schema_version, Jsonb(envelope_of(record))),
            )
        except Exception as exc:
            raise PersistenceUnavailable(f"save failed: {exc}") from exc

    def load_task(self, task_id: str) -> TaskRecord | None:
        validate_task_id(task_id)
        try:
            row = self._conn.execute(
                "SELECT record FROM nomadic_task_state WHERE task_id = %s", (task_id,)
            ).fetchone()
        except Exception as exc:
            raise PersistenceUnavailable(f"task load failed: {exc}") from exc
        if row is None:
            return None
        try:
            return decode_envelope(task_id, row[0])
        except PersistenceCorrupt:
            raise
        except Exception as exc:
            raise PersistenceCorrupt(f"task record {task_id!r} unreadable: {exc}") from exc

    def task_ids(self, limit: int = 100) -> list[str]:
        try:
            rows = self._conn.execute(
                "SELECT task_id FROM nomadic_task_state "
                "ORDER BY updated_at DESC LIMIT %s",
                (max(1, int(limit)),),
            ).fetchall()
        except Exception as exc:
            raise PersistenceUnavailable(f"task listing failed: {exc}") from exc
        return [row[0] for row in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            return None
