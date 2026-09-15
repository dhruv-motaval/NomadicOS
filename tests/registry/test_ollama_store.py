"""Ollama store discovery + migration tests (offline, synthetic store)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.registry.ollama_store import dest_path, discover, migrate, ollama_store_root


def build_store(tmp_path: Path) -> Path:
    root = tmp_path / "ollama-models"
    blobs = root / "blobs"
    blobs.mkdir(parents=True)
    # qwen3:14b — GGUF layer + config layer
    gguf1 = blobs / "sha256-a1"
    gguf1.write_bytes(b"GGUF" + b"1" * 5000)
    (blobs / "sha256-c1").write_bytes(b'{"context_length": 4096}')
    m1 = root / "manifests" / "registry.ollama.ai" / "library" / "qwen3"
    m1.mkdir(parents=True)
    # real ollama layout: the tag itself is the manifest file
    (m1 / "14b").write_text(
        json.dumps(
            {
                "layers": [
                    {"digest": "sha256:c1", "mediaType": "application/vnd.ollama.image.config"},
                    {"digest": "sha256:a1", "mediaType": "application/vnd.ollama.image.model"},
                ]
            }
        )
    )
    # custom namespace model with no explicit .model layer (largest wins)
    gguf2 = blobs / "sha256-a2"
    gguf2.write_bytes(b"GGUF" + b"2" * 9000)
    m2 = root / "manifests" / "registry.ollama.ai" / "bmichthyst" / "models"
    m2.mkdir(parents=True)
    (m2 / "latest").write_text(
        json.dumps(
            {
                "layers": [
                    {"digest": "sha256:a2", "mediaType": "application/octetstream", "size": 500_000}
                ]
            }
        )
    )
    return root


def test_store_root_missing_fails(tmp_path: Path) -> None:
    with pytest.raises(ResourceUnavailable):
        ollama_store_root(tmp_path / "nope")


def test_discover_maps_names_to_gguf_blobs(tmp_path: Path) -> None:
    root = build_store(tmp_path)
    found = discover(root)
    names = {i.name for i in found}
    assert names == {"qwen3:14b", "bmichthyst/models:latest"}
    qwen = next(i for i in found if i.name == "qwen3:14b")
    assert qwen.blob.name == "sha256-a1"  # picked the .model layer, not config
    assert qwen.size_bytes == 5004
    ns = next(i for i in found if i.name == "bmichthyst/models:latest")
    assert ns.blob.name == "sha256-a2"


def test_migrate_link_then_exists_skip(tmp_path: Path) -> None:
    root = build_store(tmp_path)
    info = discover(root)[0]
    models = tmp_path / "models"
    path, method = migrate(info, models)
    assert method in {"hardlink", "copy"}
    assert path.exists() and path.read_bytes()[:4] == b"GGUF"
    safe = info.name.replace("/", "_").replace(":", "-")
    assert dest_path(models, info) == models / f"{safe}.gguf"
    again, method2 = migrate(info, models)
    assert method2 == "exists" and again == path
    # originals untouched
    assert info.blob.exists()


def test_migrate_copy_mode_and_size_mismatch_guard(tmp_path: Path) -> None:
    root = build_store(tmp_path)
    info = discover(root)[0]
    models = tmp_path / "models"
    migrate(info, models, mode="copy")
    target = dest_path(models, info)
    target.write_bytes(b"wrong")
    with pytest.raises(ResourceUnavailable):
        migrate(info, models)
    migrate(info, models, overwrite=True)
    assert target.stat().st_size == info.size_bytes


def test_migrate_rejects_bad_mode(tmp_path: Path) -> None:
    root = build_store(tmp_path)
    info = discover(root)[0]
    with pytest.raises(ValueError):
        migrate(info, tmp_path / "m", mode="teleport")
