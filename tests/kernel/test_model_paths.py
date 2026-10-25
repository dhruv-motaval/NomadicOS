"""Model-path resolution contract tests (PR #2 review finding 3).

Contract under test (see ``NomadicApp._default_model_path`` and the README):

- a configured model-role value is resolved against the process working
  directory; absolute paths are honored as-is and are never joined onto
  ``inference.models_dir`` (no accidental ``models/models/`` join);
- relocating the models directory (``NOMADICOS_MODELS_DIR`` /
  ``inference.models_dir``) changes the ACTUAL model lookup for the
  configured role's file name, not merely the parsed configuration field;
- a configured role that resolves to a missing file is an honest absence —
  no other model file is silently substituted;
- with no role configured, the discovery directory supplies the fallback
  file for auto-start (owner-placed models directory scanning).
"""

from __future__ import annotations

from pathlib import Path

from tests.orchestration.helpers import make_app

GOAL_TEXT = "g"


def _app_with(tmp_path: Path, **config_over):
    return make_app(tmp_path, config_over=config_over)


def test_configured_role_path_is_honored_as_is(tmp_path: Path) -> None:
    """An absolute, existing role path is used as-is (explicit user path)."""
    model_file = tmp_path / "relocated" / "weights.gguf"
    model_file.parent.mkdir(parents=True, exist_ok=True)
    model_file.write_bytes(b"gguf")
    app = make_app(
        tmp_path,
        config_over={
            "inference": {"models_dir": str(tmp_path / "unused-dir")},
            "models": {"reasoning": str(model_file)},
        },
    )
    resolved = app._default_model_path()
    assert resolved is not None
    assert resolved == model_file


def test_role_path_resolves_relative_to_working_directory(tmp_path: Path, monkeypatch) -> None:
    """A relative role value resolves against the process working directory
    (repository-root-relative contract), NOT joined onto models_dir."""
    role_dir = tmp_path / "reporoot" / "models"
    role_file = role_dir / "my-model.gguf"
    role_dir.mkdir(parents=True, exist_ok=True)
    role_file.write_bytes(b"gguf")

    monkeypatch.chdir(tmp_path / "reporoot")  # role is CWD-relative
    app = make_app(
        tmp_path,
        config_over={
            "inference": {"models_dir": "models"},  # NOT joined to the role
            "models": {"reasoning": "models/my-model.gguf"},
        },
    )
    # resolved against the working directory (tmp_path/"reporoot") — the
    # models_dir was not prepended to the role path (no models/models/ join)
    resolved = app._default_model_path()
    assert resolved == Path("models/my-model.gguf")
    assert (Path.cwd() / "models" / "my-model.gguf") == role_file
    assert str(resolved) == str(Path("models/my-model.gguf"))


def test_models_dir_override_relocates_actual_lookup(tmp_path: Path) -> None:
    """Relocating the models directory changes the ACTUAL lookup for a
    configured role's file name (not merely the parsed config value), and
    the models/ prefix in the role is never double-joined."""
    relocated = tmp_path / "relocated-models"
    relocated.mkdir()
    model_file = relocated / "test.gguf"
    model_file.write_bytes(b"gguf")

    app = make_app(
        tmp_path,
        config_over={
            "inference": {"models_dir": str(relocated)},
            "models": {"reasoning": "models/test.gguf"},  # missing relative to CWD
        },
    )
    resolved = app._default_model_path()
    assert resolved == relocated / "test.gguf"
    # no accidental double join: "models/models/test.gguf" never resolved
    assert "models" + "/" + "models" not in str(resolved)


def test_missing_model_file_is_honest_absence(tmp_path: Path) -> None:
    """A configured role that resolves to a missing file must NOT be
    substituted with another model file (honest absence, §12)."""
    empty_dir = tmp_path / "empty-models"
    empty_dir.mkdir()
    app = make_app(
        tmp_path,
        config_over={
            "inference": {"models_dir": str(empty_dir)},
            "models": {"reasoning": str(tmp_path / "absent" / "missing.gguf")},
        },
    )
    assert app._default_model_path() is None


def test_models_dir_discovery_when_no_role_configured(tmp_path: Path) -> None:
    """With no model role configured, the discovery directory supplies the
    fallback file (first sorted *.gguf) — the documented scan behavior."""
    models_dir = tmp_path / "scanned"
    models_dir.mkdir()
    (models_dir / "b-first.Q4.gguf").write_bytes(b"gguf")
    (models_dir / "a-second.Q4.gguf").write_bytes(b"gguf")
    app = make_app(
        tmp_path,
        config_over={
            "inference": {"models_dir": str(models_dir)},
            "models": {"reasoning": None, "coding_worker": None, "worker": None},
        },
    )
    resolved = app._default_model_path()
    assert resolved is not None
    assert resolved.parent == models_dir
    assert resolved.name == "a-second.Q4.gguf"  # sorted scan fallback
