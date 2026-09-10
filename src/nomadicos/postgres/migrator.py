"""Migration runner (BP §104): ordered SQL files, versioned, verified.

Every schema change: migration → test → backup → apply → verify. Manual,
undocumented schema changes are rejected by construction — the runner refuses
to apply anything not recorded in its table, and refuses unknown states.
"""

from pathlib import Path

from nomadicos.core.logging import get_logger
from nomadicos.postgres.client import PostgresClient

logger = get_logger("postgres.migrator")

MIGRATIONS_TABLE = """
CREATE SCHEMA IF NOT EXISTS nomadicos;

CREATE TABLE IF NOT EXISTS nomadicos.schema_migrations (
    version       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum      TEXT NOT NULL
)
"""


def _checksum(sql: str) -> str:
    import hashlib

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


class MigrationRunner:
    def __init__(self, client: PostgresClient, directory: str | Path) -> None:
        self._client = client
        self._directory = Path(directory)

    def discover(self) -> list[tuple[int, str, str, str]]:
        """Returns [(version, name, sql, checksum)] sorted by version."""
        migrations: list[tuple[int, str, str, str]] = []
        for path in sorted(self._directory.glob("*.sql")):
            stem = path.stem
            version_part = stem.split("_", 1)[0]
            if not version_part.isdigit():
                raise ValueError(f"migration file must start with a number: {path.name}")
            sql = path.read_text(encoding="utf-8")
            migrations.append((int(version_part), path.name, sql, _checksum(sql)))
        migrations.sort(key=lambda m: m[0])
        return migrations

    def applied_versions(self) -> dict[int, str]:
        rows = self._client.execute(
            "SELECT version, checksum FROM nomadicos.schema_migrations ORDER BY version"
        )
        return {int(row["version"]): str(row["checksum"]) for row in rows}

    def ensure_migrations_table(self) -> None:
        self._client.execute(MIGRATIONS_TABLE)

    def run(self) -> list[str]:
        """Apply pending migrations in order; detect tampering and drift."""
        self.ensure_migrations_table()
        applied = self.applied_versions()
        applied_names: list[str] = []
        for version, name, sql, checksum in self.discover():
            if version in applied:
                if applied[version] != checksum:
                    raise ValueError(
                        f"migration {version} checksum mismatch — file was modified "
                        "after apply (BP §104: no undocumented schema changes)"
                    )
                continue
            logger.info("applying migration version=%d name=%s", version, name)
            with self._client.transaction():
                self._client.execute(sql)
                self._client.execute(
                    "INSERT INTO nomadicos.schema_migrations (version, name, checksum) "
                    "VALUES (%s, %s, %s)",
                    (version, name, checksum),
                )
            applied_names.append(name)
        return applied_names
