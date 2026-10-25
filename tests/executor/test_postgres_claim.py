"""PostgreSQL execution-intent claim atomicity (Phase 4C, SPEC §56.14).

Integration-marked: requires a reachable PostgreSQL (the claim's atomicity
is the DATABASE's unique-constraint + INSERT ... ON CONFLICT DO NOTHING
semantics, which are process-independent — unlike the file ledger's lock
file, two OS processes are not required to demonstrate the guarantee; two
independent connections racing at the server are equivalent evidence).
Skips cleanly when the database is unavailable — never faked.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from nomadicos.contracts.execution import ExecutionIntent
from nomadicos.persistence.errors import PersistenceUnavailable
from nomadicos.persistence.ledger import PostgresExecutionLedger

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


def _fresh_key() -> str:
    """Unique 32-hex key per invocation: the dev database PERSISTS intent
    rows across test runs, so fixed keys would collide with a prior run."""
    return uuid.uuid4().hex[:32]


def _ledger() -> PostgresExecutionLedger:
    try:
        return PostgresExecutionLedger(TEST_DSN, connect_timeout_s=2)
    except PersistenceUnavailable as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc.message[:100]}")


def _intent(key: str) -> ExecutionIntent:
    return ExecutionIntent(
        task_id="task-pg-claim",
        step_id="s0",
        fingerprint=key,
        authorization_epoch=1,
        tool="filesystem",
        operation="write",
        args={"path": "x.txt", "content": "y"},
    )


def test_postgres_claim_first_writer_wins() -> None:
    """Two independent connections, sequential claims on one key: the second
    INSERT ... ON CONFLICT DO NOTHING returns no row -> False."""
    a = _ledger()
    b = _ledger()
    try:
        key = _fresh_key()
        assert a.claim_intent(key, _intent(key)) is True
        assert b.claim_intent(key, _intent(key)) is False
        # the winner's record is durable and readable from either connection
        loaded = b.get_intent(key)
        assert loaded is not None
        assert loaded.task_id == "task-pg-claim"
    finally:
        a.close()
        b.close()


def test_postgres_claim_concurrent_exactly_one_winner() -> None:
    """Two independent connections racing on the same key from two threads
    at the same instant: the server's unique constraint elects exactly one
    winner no matter how the inserts interleave."""
    a = _ledger()
    b = _ledger()
    try:
        key = _fresh_key()
        results: dict[str, bool] = {}
        barrier = threading.Barrier(2)

        def _claim(name: str, ledger: PostgresExecutionLedger) -> None:
            barrier.wait(timeout=10)  # both threads fire the INSERT together
            results[name] = ledger.claim_intent(key, _intent(key))

        ta = threading.Thread(target=_claim, args=("a", a))
        tb = threading.Thread(target=_claim, args=("b", b))
        ta.start()
        tb.start()
        ta.join(timeout=30)
        tb.join(timeout=30)
        assert len(results) == 2
        assert sum(1 for won in results.values() if won) == 1, (
            f"exactly one claimant must win, got {results}"
        )
    finally:
        a.close()
        b.close()
