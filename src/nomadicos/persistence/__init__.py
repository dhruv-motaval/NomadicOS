"""Persistence brick (Phase 13A, SPEC 34): durable task-state contracts +
stores. Persisted data is DATA - never authority, never SUCCESS proof."""

from nomadicos.persistence.contracts import (
    AuditEventRecord,
    AuthorizedActionRef,
    TaskRecord,
    new_record,
    now_iso,
    validate_task_id,
)
from nomadicos.persistence.errors import (
    PersistenceCorrupt,
    PersistenceError,
    PersistenceUnavailable,
)
from nomadicos.persistence.postgres import PostgresTaskStateStore
from nomadicos.persistence.store import JsonTaskStateStore, TaskStateStore

SCHEMA_VERSION = 1


def make_task_store(persistence) -> TaskStateStore:
    """Configuration-selected durable task-state store (SPEC 34): PostgreSQL
    when PersistenceConfig.dsn is set, otherwise the deterministic file
    adapter under state_dir."""
    if persistence is not None and persistence.dsn:
        return PostgresTaskStateStore(persistence.dsn)
    state_dir = getattr(persistence, "state_dir", None) or "data/state"
    return JsonTaskStateStore(state_dir)


__all__ = [
    "AuditEventRecord",
    "AuthorizedActionRef",
    "JsonTaskStateStore",
    "PersistenceCorrupt",
    "PersistenceError",
    "PersistenceUnavailable",
    "PostgresTaskStateStore",
    "SCHEMA_VERSION",
    "TaskRecord",
    "TaskStateStore",
    "make_task_store",
    "new_record",
    "now_iso",
    "validate_task_id",
]
