"""psycopg 3 connection wrapper (ADR-0010). No raw SQL outside postgres/."""

from typing import Any

import psycopg
from psycopg.rows import dict_row

from nomadicos.core.config import CoreConfig, PostgresConfig
from nomadicos.core.errors import NomadicError
from nomadicos.core.logging import get_logger

logger = get_logger("postgres")


class DatabaseUnavailable(NomadicError):
    """PostgreSQL unreachable (BP §237: degrade safely, never permissive)."""


class PostgresClient:
    """Owns the connection; repositories execute domain SQL through it.

    Agents and models never touch this module directly (BP §93).
    """

    def __init__(self, config: PostgresConfig, password: str) -> None:
        self._config = config
        self._password = password
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    def connect(self) -> None:
        """Connect using keyword arguments — safe for passwords with special
        characters (a DSN string would mis-parse them)."""
        try:
            self._conn = psycopg.connect(
                host=self._config.host,
                port=self._config.port,
                dbname=self._config.db,
                user=self._config.user,
                password=self._password,
                row_factory=dict_row,
                autocommit=False,
            )
        except psycopg.errors.InvalidPassword as exc:
            raise DatabaseUnavailable(
                "password authentication failed — check POSTGRES_PASSWORD in .env "
                "against the password set in the PostgreSQL installer",
                context={"host": self._config.host, "user": self._config.user},
            ) from exc
        except psycopg.errors.InvalidCatalogName as exc:
            raise DatabaseUnavailable(
                f"database {self._config.db!r} does not exist — create it in pgAdmin",
                context={"host": self._config.host, "database": self._config.db},
            ) from exc
        except psycopg.OperationalError as exc:
            hint = (
                "is the service running? Check pgAdmin → Servers, "
                "or services.msc → postgresql-x64-17"
            )
            raise DatabaseUnavailable(
                f"cannot reach PostgreSQL at {self._config.host}:{self._config.port} — {hint}",
                context={"host": self._config.host, "port": self._config.port},
            ) from exc

    def execute(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        """Run one statement; commits only when not already inside an explicit
        transaction (BP §250)."""
        if not self.connected:
            raise DatabaseUnavailable("not connected")
        assert self._conn is not None
        in_transaction = self._conn._num_transactions > 0  # noqa: SLF001
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows: list[dict[str, Any]] = cur.fetchall() if cur.description else []
        if not in_transaction:
            self._conn.commit()
        return rows

    def transaction(self) -> Any:
        """Explicit transaction for logically atomic state changes (BP §250)."""
        if not self.connected or self._conn is None:
            raise DatabaseUnavailable("not connected")
        return self._conn.transaction()

    def rollback(self) -> None:
        """Abandon the current implicit transaction after an error."""
        if self.connected and self._conn is not None:
            self._conn.rollback()

    def ping(self) -> bool:
        try:
            self.execute("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001 — health check reports, never raises
            return False

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


def client_from_config(config: CoreConfig | PostgresConfig, password: str) -> PostgresClient:
    cfg = config.postgres if isinstance(config, CoreConfig) else config
    return PostgresClient(cfg, password)


__all__ = ["DatabaseUnavailable", "PostgresClient", "client_from_config"]
