"""Integration tests against real PostgreSQL (ADR-0009/0010).

Skipped when the database is unreachable so shared CI stays green; run locally
via `docker compose -f docker-compose.dev.yml up -d`.
"""

import os
from pathlib import Path

import pytest

from nomadicos.audit.base import AuditEvent, AuditEventCategory
from nomadicos.core.config import CoreConfig
from nomadicos.core.errors import ValidationError
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.postgres.client import PostgresClient, client_from_config
from nomadicos.postgres.migrator import MigrationRunner
from nomadicos.postgres.repositories import (
    AuditRepository,
    ModelRepository,
    SessionRepository,
    TaskRepository,
)

POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "nomadicos-dev")
MIGRATIONS = Path(__file__).parents[2] / "src/nomadicos/postgres/migrations"


def _client() -> PostgresClient:
    config = CoreConfig()
    client = client_from_config(config, POSTGRES_PASSWORD)
    client.connect()
    if not client.ping():
        raise ConnectionError("no database")
    return client


@pytest.fixture()
def db():
    try:
        client = _client()
    except Exception:  # noqa: BLE001 — skip when Docker/PG unavailable
        pytest.skip("PostgreSQL not reachable (ADR-0009 dev container down)")
    # Clean state for deterministic tests
    client.execute("DROP SCHEMA IF EXISTS nomadicos CASCADE")
    yield client
    client.close()


def test_migrations_apply_and_are_idempotent(db) -> None:
    runner = MigrationRunner(db, MIGRATIONS)
    applied_first = runner.run()
    assert applied_first == [
    "001_core_state.sql",
    "002_memory.sql",
    "003_audit_correlation_text.sql",
]
    applied_second = runner.run()
    assert applied_second == []  # idempotent


def test_session_and_task_lifecycle(db) -> None:
    MigrationRunner(db, MIGRATIONS).run()

    sessions = SessionRepository(db)
    tasks = TaskRepository(db)

    session_id = __import__("asyncio").run(
        sessions.create("user-1", title="test session")
    )
    session = __import__("asyncio").run(sessions.get(session_id))
    assert session["user_id"] == "user-1"

    task_id = __import__("asyncio").run(
        tasks.create("user-1", "Open VS Code and run the tests", session_id=session_id)
    )
    task = __import__("asyncio").run(tasks.get(task_id))
    assert str(task["session_id"]) == str(session_id)  # ADR-0026/F5 correlation
    assert task["status"] == "PLANNED"

    with pytest.raises(ValidationError, match="non-empty"):
        __import__("asyncio").run(tasks.create("user-1", "   "))

    run_id = __import__("asyncio").run(tasks.start_run(task_id))
    __import__("asyncio").run(tasks.finish_run(run_id, {"ok": True}))

    # repository refuses non-terminal status writes (state machine owns those)
    with pytest.raises(ValidationError):
        __import__("asyncio").run(tasks.set_status(task_id, TaskStatus.RUNNING))
    __import__("asyncio").run(tasks.set_status(task_id, TaskStatus.SUCCESS))


def test_model_repository_upsert_and_performance(db) -> None:
    MigrationRunner(db, MIGRATIONS).run()
    repo = ModelRepository(db)

    __import__("asyncio").run(
        repo.upsert(
            {
                "model_id": "llama/3.2-3b",
                "display_name": "Llama 3.2 3B",
                "path": "models/llama3.2-3b.gguf",
                "status": "enabled",
                "capabilities": {"text_generation": True, "vision": False},
                "resources": {"min_ram_mb": 4096},
            }
        )
    )
    enabled = __import__("asyncio").run(repo.list_enabled())
    assert [m["model_id"] for m in enabled] == ["llama/3.2-3b"]

    __import__("asyncio").run(
        repo.record_performance("llama/3.2-3b", "coding", success=True, latency_ms=1200.0)
    )
    __import__("asyncio").run(
        repo.record_performance("llama/3.2-3b", "coding", success=True, latency_ms=1000.0)
    )
    rows = db.execute(
        "SELECT * FROM nomadicos.model_performance WHERE model_id = %s", ("llama/3.2-3b",)
    )
    assert rows[0]["attempt_count"] == 2
    assert rows[0]["success_count"] == 2
    assert abs(rows[0]["avg_latency_ms"] - 1180.0) < 1.0  # EMA α=0.1: 1200*0.9 + 1000*0.1


def test_audit_is_append_only(db) -> None:
    MigrationRunner(db, MIGRATIONS).run()
    repo = AuditRepository(db)

    event = AuditEvent(
        category=AuditEventCategory.TOOL_REQUESTED,
        subject="filesystem.read",
        decision="ALLOW",
    )
    __import__("asyncio").run(repo.append(event))
    events = __import__("asyncio").run(
        repo.query(category=AuditEventCategory.TOOL_REQUESTED)
    )
    assert len(events) == 1
    assert events[0].subject == "filesystem.read"

    # append-only at the database level (BP §41-42, I7)
    with pytest.raises(Exception, match="append-only"):
        db.execute("UPDATE nomadicos.audit_events SET decision = 'TAMPERED'")
    db.rollback()
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM nomadicos.audit_events")
