"""Phase 13A PostgreSQL integration tests (SPEC Â§34).

Integration-marked: requires a reachable PostgreSQL. Skips cleanly when the
database is unavailable â€” never faked with the file adapter. Dev database
comes from docker-compose.dev.yml (dev-only credentials, not secrets).
"""

from __future__ import annotations

import os

import pytest

from nomadicos.persistence.contracts import new_record
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceUnavailable
from nomadicos.persistence.postgres import PostgresTaskStateStore

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


def _store() -> PostgresTaskStateStore:
    try:
        return PostgresTaskStateStore(TEST_DSN, connect_timeout_s=2)
    except PersistenceUnavailable as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc.message[:100]}")


def test_postgres_round_trip() -> None:

    store = _store()
    try:
        record = new_record("task_" + "1" * 20, "Durable objective")
        store.save_task(record)
        loaded = store.load_task(record.task_id)
        assert loaded is not None
        assert loaded.objective == "Durable objective"
        assert loaded.status is record.status
    finally:
        store.close()


def test_postgres_update_and_list() -> None:
    store = _store()
    record = new_record("task_" + "d" * 20, "Update me")
    store.save_task(record)
    store.save_task(record.model_copy(update={"outcome_note": "updated"}))
    loaded = store.load_task(record.task_id)
    assert loaded.outcome_note == "updated"
    assert record.task_id in store.task_ids(limit=50)
    store.close()


def test_postgres_cross_task_isolation() -> None:
    store = _store()
    a = new_record("task_" + "a" * 20, "PG task A")
    b = new_record("task_" + "2" * 20, "PG task B")
    store.save_task(a)
    store.save_task(b)
    assert store.load_task("task_" + "a" * 20).objective == "PG task A"
    assert store.load_task("task_" + "2" * 20).objective == "PG task B"
    store.close()


def test_postgres_corrupt_record_fails_closed() -> None:
    store = _store()
    record = new_record("task_" + "c" * 20, "objective")
    store.save_task(record)
    store._conn.execute(
        "UPDATE nomadic_task_state SET record = %s WHERE task_id = %s",
        ('{"kind": "task_state", "schema_version": 1, "record": {"task_id": "WRONG"}}',
         record.task_id),
    )
    with pytest.raises(PersistenceCorrupt):
        store.load_task(record.task_id)
    store.close()


def test_postgres_schema_version_mismatch_fails_closed() -> None:
    store = _store()
    record = new_record("task_" + "c" * 20, "objective")
    store.save_task(record)
    # corrupt the versioned ENVELOPE (what the loader actually reads)
    store._conn.execute(
        "UPDATE nomadic_task_state SET record = jsonb_set(record, '{schema_version}', '99') "
        "WHERE task_id = %s",
        (record.task_id,),
    )
    with pytest.raises(Exception, match="version"):
        store.load_task(record.task_id)
    store.close()


def test_postgres_unavailable_is_structured() -> None:
    with pytest.raises(PersistenceUnavailable):
        PostgresTaskStateStore(
            "postgresql://nomadicos:nomadicos@localhost:9999/nomadicos", connect_timeout_s=1
        )

