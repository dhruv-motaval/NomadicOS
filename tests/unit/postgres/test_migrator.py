"""Migration integrity tests (BP §104) — no database required."""

from pathlib import Path  # noqa: F401

import pytest

from nomadicos.postgres.client import PostgresClient
from nomadicos.postgres.migrator import MigrationRunner, _checksum


class FakeClient:
    """Records executed SQL without a server."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | dict | None]] = []
        self._table = False
        self._applied: dict[int, str] = {}

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "schema_migrations" in sql and "CREATE TABLE" in sql:
            self._table = True
        return []

    def transaction(self):
        import contextlib

        @contextlib.contextmanager
        def _tx():
            yield None

        return _tx()


def test_discovery_requires_versioned_names(tmp_path) -> None:
    (tmp_path / "not_numbered.sql").write_text("SELECT 1;", encoding="utf-8")
    runner = MigrationRunner(FakeClient(), tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="number"):
        runner.discover()


def test_discovery_sorts_by_version(tmp_path) -> None:
    (tmp_path / "002_b.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    runner = MigrationRunner(FakeClient(), tmp_path)  # type: ignore[arg-type]
    versions = [m[0] for m in runner.discover()]
    assert versions == [1, 2]


def test_checksum_detects_post_apply_modification(tmp_path) -> None:
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    client = FakeClient()
    runner = MigrationRunner(client, tmp_path)  # type: ignore[arg-type]

    # Simulate already-applied with a different checksum (tampering).
    runner.ensure_migrations_table = lambda: None  # type: ignore[method-assign]
    applied = {1: "deadbeef"}
    object.__setattr__(runner, "applied_versions", lambda: applied)

    with pytest.raises(ValueError, match="checksum mismatch"):
        runner.run()


def test_checksum_is_deterministic() -> None:
    assert _checksum("SELECT 1;") == _checksum("SELECT 1;")
    assert _checksum("SELECT 1;") != _checksum("SELECT 2;")


def test_migration_001_contains_core_tables() -> None:
    migrations_dir = Path(__file__).parents[3] / "src/nomadicos/postgres/migrations"
    sql = (migrations_dir / "001_core_state.sql").read_text(encoding="utf-8")
    # core BP §19/§206 tables present
    for table in ("sessions", "tasks", "task_runs", "models", "audit_events", "experiences"):
        assert f"nomadicos.{table}" in sql
    # F5: tasks carry session_id (ADR-0026)
    assert "session_id" in sql
    # audit is append-oriented: no ON CONFLICT upsert on audit_events
    assert "ON CONFLICT" not in sql.split("audit_events")[1].split(");")[0]


def test_client_requires_connect_before_execute() -> None:
    client = PostgresClient.__new__(PostgresClient)
    client._conn = None
    client._config = None
    client._password = None
    with pytest.raises(Exception, match="not connected"):
        client.execute("SELECT 1")
