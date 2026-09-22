"""Phase 13D PostgreSQL ledger integration tests (SPEC 34).

Integration-marked: requires a reachable PostgreSQL per docker-compose.dev.yml.
Skips cleanly when unavailable; never faked with the file adapter.
"""

from __future__ import annotations

import os

import pytest

from nomadicos.contracts.execution import ExecutionResult
from nomadicos.persistence.errors import PersistenceUnavailable
from nomadicos.persistence.ledger import PostgresExecutionLedger

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5432/nomadicos"
)

pytestmark = pytest.mark.integration

KEY = "b" * 32


def _store() -> PostgresExecutionLedger:
    try:
        return PostgresExecutionLedger(TEST_DSN, connect_timeout_s=2)
    except PersistenceUnavailable as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc.message[:100]}")


def _result() -> ExecutionResult:
    return ExecutionResult(
        task_id="task_pg000000000000000001",
        step_id="s1",
        action_id="act_1",
        action_fingerprint="fp",
        model_id="m",
        tool="desktop",
        operation="mouse_click",
        status="SUCCEEDED",
        evidence={"x": 1, "y": 2},
    )


def test_postgres_ledger_round_trip() -> None:
    store = _store()
    try:
        assert store.get(KEY) is None
        store.put(KEY, _result())
        loaded = store.get(KEY)
        assert loaded is not None
        assert loaded.task_id == "task_pg000000000000000001"
        assert loaded.evidence["x"] == 1
    finally:
        store.close()


def test_postgres_ledger_first_writer_wins() -> None:
    store = _store()
    try:
        store.put(KEY, _result())
        store.put(KEY, _result().model_copy(update={"status": "FAILED"}))
        loaded = store.get(KEY)
        assert loaded.status.value == "SUCCEEDED"
    finally:
        store.close()


def test_postgres_ledger_unavailable_is_structured() -> None:
    with pytest.raises(PersistenceUnavailable):
        PostgresExecutionLedger(
            "postgresql://nomadicos:nomadicos@localhost:9999/nomadicos", connect_timeout_s=1
        )
