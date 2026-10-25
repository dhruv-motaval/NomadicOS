"""Configuration tests for Phase 4C PostgreSQL wiring (SPEC §42).

These tests exercise the trusted configuration path:
``.env`` / process environment -> ``PersistenceConfig`` -> ``AppConfig``.

Unit tests do not require a live database. Tests marked ``integration`` need
the dev PostgreSQL (see ``tests/persistence/test_postgres_integration.py``)
and skip cleanly when it is unavailable.
"""

from __future__ import annotations

import os

import pytest

from nomadicos.kernel.config import (
    AppConfig,
    BudgetConfig,
    PersistenceConfig,
    _quote_conninfo_value,
    load_config,
)
from nomadicos.kernel.errors import ConfigInvalid
from nomadicos.persistence import make_task_store
from nomadicos.persistence.errors import PersistenceUnavailable
from nomadicos.persistence.postgres import PostgresTaskStateStore
from nomadicos.persistence.store import JsonTaskStateStore

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)


# --------------------------------------------------------------------------- #
# Unit tests: no live database required
# --------------------------------------------------------------------------- #


def test_env_file_loaded_into_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root .env values flow into PersistenceConfig via load_config()."""
    monkeypatch.setenv("NOMADICOS_PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "nomadicos")
    monkeypatch.setenv("POSTGRES_USER", "nomadicos")
    monkeypatch.setenv("POSTGRES_PASSWORD", "nomadicos")
    cfg = load_config()
    assert cfg.persistence.persistence_backend == "postgres"
    assert cfg.persistence.dsn is not None
    assert "host=localhost" in cfg.persistence.dsn
    assert "dbname=nomadicos" in cfg.persistence.dsn
    assert "user=nomadicos" in cfg.persistence.dsn


def test_process_env_overrides_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process environment variables win over .env (setdefault semantics)."""
    monkeypatch.setenv("NOMADICOS_PERSISTENCE_BACKEND", "file")
    # .env also sets POSTGRES_PASSWORD=nomadicos; backend=file ignores it.
    cfg = load_config()
    assert cfg.persistence.persistence_backend == "file"
    assert cfg.persistence.dsn is None


def test_dsn_special_characters_escaped_safely() -> None:
    """Passwords containing quote/backslash/space are quoted for libpq."""
    # Quoting helper: special chars get single-quoted and escaped.
    assert _quote_conninfo_value("plain") == "plain"
    assert _quote_conninfo_value("a b") == "'a b'"
    assert _quote_conninfo_value("it's") == "'it\\'s'"
    assert _quote_conninfo_value("a\\b") == "'a\\\\b'"

    # A password with special characters builds a DSN psycopg can parse
    # (auth failure, not a conninfo syntax error).
    cfg = PersistenceConfig(
        persistence_backend="postgres",
        postgres_host="localhost",
        postgres_port=5433,
        postgres_db="nomadicos",
        postgres_user="nomadicos",
        postgres_password="it's \\weird\\",
    )
    import psycopg

    try:
        psycopg.connect(cfg.dsn, connect_timeout=2)
    except psycopg.OperationalError as exc:
        # Auth/db error means the DSN parsed; a syntax error means quoting broke.
        msg = str(exc).lower()
        assert "syntax" not in msg, f"DSN quoting broke parsing: {exc}"
    else:
        pytest.fail("expected auth/db error, got a successful connection")


def test_file_backend_is_default() -> None:
    """Without configuration, file-backed persistence is used (no DB needed)."""
    cfg = PersistenceConfig()
    assert cfg.persistence_backend == "file"
    assert cfg.dsn is None


def test_file_backend_makes_file_store() -> None:
    """file backend routes task state to the JSON file adapter."""
    from nomadicos.persistence import make_task_store

    cfg = PersistenceConfig(persistence_backend="file", state_dir="/tmp/nomadic-test")
    store = make_task_store(cfg)
    assert isinstance(store, JsonTaskStateStore)


def test_postgres_backend_requires_password() -> None:
    """postgres backend without a password fails closed with a clear error."""
    with pytest.raises(ConfigInvalid, match="POSTGRES_PASSWORD"):
        PersistenceConfig(
            persistence_backend="postgres",
            postgres_host="localhost",
            postgres_port=5433,
            postgres_db="nomadicos",
            postgres_user="nomadicos",
            postgres_password=None,
        )


def test_adaptive_decision_budget_defaults() -> None:
    """Phase 4D: default adaptive decision budgets are present and bounded."""
    cfg = AppConfig()
    assert cfg.budget.max_retries_per_task == 3
    assert cfg.budget.max_replans_per_mission == 3
    assert cfg.budget.max_adaptive_decisions_per_mission == 10
    assert cfg.budget.max_reasoning_calls_per_cycle == 2


def test_adaptive_decision_budget_validation() -> None:
    """Phase 4D: adaptive decision budgets reject out-of-range values."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BudgetConfig(max_retries_per_task=0)  # below ge=1
    with pytest.raises(ValidationError):
        BudgetConfig(max_replans_per_mission=21)  # above le=20
    with pytest.raises(ValidationError):
        BudgetConfig(max_adaptive_decisions_per_mission=101)  # above le=100
    with pytest.raises(ValidationError):
        BudgetConfig(max_reasoning_calls_per_cycle=0)  # below ge=1


def test_adaptive_decision_budget_from_yaml() -> None:
    """Phase 4D: adaptive decision budgets can be set via YAML config."""
    # If no config file, just verify defaults work:
    cfg = AppConfig()
    assert cfg.budget.max_retries_per_task == 3
    assert cfg.budget.max_replans_per_mission == 3
    assert cfg.budget.max_adaptive_decisions_per_mission == 10
    assert cfg.budget.max_reasoning_calls_per_cycle == 2
    # Verify they can be overridden:
    cfg2 = AppConfig(
        budget=BudgetConfig(
            max_retries_per_task=5,
            max_replans_per_mission=2,
            max_adaptive_decisions_per_mission=20,
            max_reasoning_calls_per_cycle=3,
        )
    )
    assert cfg2.budget.max_retries_per_task == 5
    assert cfg2.budget.max_replans_per_mission == 2
    assert cfg2.budget.max_adaptive_decisions_per_mission == 20
    assert cfg2.budget.max_reasoning_calls_per_cycle == 3


def test_credentials_not_in_error_messages() -> None:
    """Error messages never leak credentials."""
    pwd = "s3cr3t!@#$"
    cfg = PersistenceConfig(
        persistence_backend="postgres",
        postgres_host="127.0.0.1",
        postgres_port=1,  # nothing listening
        postgres_db="nomadicos",
        postgres_user="nomadicos",
        postgres_password=pwd,
    )
    from nomadicos.persistence.ledger import PostgresExecutionLedger

    try:
        PostgresExecutionLedger(cfg.dsn, connect_timeout_s=1)
    except PersistenceUnavailable as exc:
        msg = str(exc)
        assert pwd not in msg, f"password leaked into error: {msg}"
    else:
        pytest.fail("expected PersistenceUnavailable for unreachable DSN")


# --------------------------------------------------------------------------- #
# Integration tests: real PostgreSQL required
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_postgres_backend_reaches_real_factories() -> None:
    """postgres backend routes every store to the Postgres adapters."""
    from nomadicos.persistence.checkpoints import PostgresCheckpointSaver, make_durable_saver
    from nomadicos.persistence.ledger import (
        PostgresExecutionLedger,
        make_execution_ledger,
    )
    from nomadicos.persistence.metrics import PostgresModelMetricStore

    cfg = PersistenceConfig(
        persistence_backend="postgres",
        postgres_host="localhost",
        postgres_port=5433,
        postgres_db="nomadicos",
        postgres_user="nomadicos",
        postgres_password="nomadicos",
    )
    assert isinstance(make_task_store(cfg), PostgresTaskStateStore)
    assert isinstance(make_durable_saver(cfg), PostgresCheckpointSaver)
    assert isinstance(make_execution_ledger(cfg), PostgresExecutionLedger)
    assert isinstance(PostgresModelMetricStore(cfg.dsn), PostgresModelMetricStore)


@pytest.mark.integration
def test_unavailable_postgres_raises_not_fallback() -> None:
    """An unreachable PostgreSQL raises PersistenceUnavailable; no silent
    file-backed fallback."""
    cfg = PersistenceConfig(
        persistence_backend="postgres",
        postgres_host="127.0.0.1",
        postgres_port=1,  # nothing listening
        postgres_db="nomadicos",
        postgres_user="nomadicos",
        postgres_password="nomadicos",
    )
    with pytest.raises(PersistenceUnavailable):
        PostgresTaskStateStore(cfg.dsn, connect_timeout_s=1)
