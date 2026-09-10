"""Typed configuration with fail-closed validation (BP §102, §85, ADR-0023).

Configuration is layered: defaults → `config/*.yaml` → environment (BP §103
variables only carry deployment-level settings). Invalid configuration aborts
startup; nothing is coerced or defaulted to permissive.
"""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nomadicos.core.errors import ValidationError


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    db: str = "nomadicos"
    user: str = "nomadicos"
    password_env: str = "POSTGRES_PASSWORD"  # never an inline secret (ADR-0021)

    def dsn(self, password: str) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.db} "
            f"user={self.user} password={password}"
        )


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")
    model_dir: Path = Path("models")

    @property
    def vector_dir(self) -> Path:
        return self.data_dir / "vector"  # ADR-0008

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


class CoreConfig(BaseModel):
    """Top-level typed configuration (BP §102: do not scatter configuration)."""

    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "production"] = "development"
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @field_validator("paths")
    @classmethod
    def _absolute(cls, value: PathsConfig) -> PathsConfig:
        return value.model_copy(
            update={
                "data_dir": value.data_dir.resolve(),
                "model_dir": value.model_dir.resolve(),
            }
        )


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> CoreConfig:
    """Load config from YAML (+ optional override dict) and validate. Fail closed."""
    raw: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            if loaded is not None:
                if not isinstance(loaded, dict):
                    raise ValidationError("configuration root must be a mapping")
                raw = loaded
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(
                f"invalid configuration file {path}: {type(exc).__name__}"
            ) from exc
    if overrides:
        raw.update(overrides)
    try:
        return CoreConfig.model_validate(raw)
    except Exception as exc:
        raise ValidationError(f"invalid configuration: {exc}") from exc
