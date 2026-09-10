import pytest
import yaml

from nomadicos.core.config import CoreConfig, load_config
from nomadicos.core.errors import ValidationError


def test_defaults_are_conservative() -> None:
    config = CoreConfig()
    assert config.environment == "development"
    assert config.postgres.password_env == "POSTGRES_PASSWORD"  # no inline secret (ADR-0021)
    assert config.paths.vector_dir.name == "vector"  # ADR-0008 layout


def test_load_config_from_yaml(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
environment: development
logging:
  level: DEBUG
postgres:
  host: db.internal
  port: 5433
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.logging.level == "DEBUG"
    assert config.postgres.host == "db.internal"
    assert config.postgres.port == 5433


def test_invalid_config_fails_closed(tmp_path) -> None:
    # Unparseable YAML
    bad = tmp_path / "bad.yaml"
    bad.write_text("logging: [unclosed", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(bad)

    # Unknown field rejected (strict models, BP §102)
    mystery = tmp_path / "mystery.yaml"
    mystery.write_text(yaml.safe_dump({"mystery_section": {"x": 1}}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(mystery)

    # Non-mapping root rejected
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("42", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(scalar)


def test_overrides_apply() -> None:
    config = load_config(overrides={"logging": {"level": "ERROR"}})
    assert config.logging.level == "ERROR"
