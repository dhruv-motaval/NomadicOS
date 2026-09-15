"""Ollama store migration (owner directive 2026-09-15).

NomadicOS' primary engine is llama.cpp, which serves GGUF files from
``models/``. Ollama already keeps every pulled model's GGUF layer as a
content-addressed blob (``~/.ollama/models/blobs/sha256-...``) referenced by
manifests here — no download, no re-quantization, originals untouched:

- ``link`` mode (default): NTFS hardlink — instant, zero extra disk.
- ``copy`` mode: real copy when crossing volumes / OneDrive placeholders.

Hardlink/copy failures are reported honestly; nothing is faked (SPEC §51).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel

from nomadicos.kernel.errors import ResourceUnavailable


class OllamaModelInfo(BaseModel):
    name: str  # e.g. "qwen3:14b"
    tag: str
    size_bytes: int
    blob: Path
    media_type: str = "application/vnd.ollama.image.model"


def ollama_store_root(store: str | Path | None = None) -> Path:
    root = (
        Path(store)
        if store
        else Path(os.environ.get("OLLAMA_MODELS", Path.home() / ".ollama" / "models"))
    )
    if not root.exists():
        raise ResourceUnavailable(f"ollama store not found at {root}")
    return root


def _digest_filename(digest: str) -> str:
    alg, _, hexpart = digest.partition(":")
    return f"{alg}-{hexpart}"


def discover(store: str | Path | None = None) -> list[OllamaModelInfo]:
    """Parse Ollama manifests into model->GGUF blob mappings."""
    root = ollama_store_root(store)
    manifests = root / "manifests"
    blobs = root / "blobs"
    found: dict[str, OllamaModelInfo] = {}
    if not manifests.exists():
        return []
    for manifest in manifests.rglob("*"):
        if not manifest.is_file():
            continue
        # layout: manifests/registry.ollama.ai/[library/]<name...>/<tag>
        rel_parts = manifest.relative_to(manifests).parts
        if len(rel_parts) < 3 or rel_parts[0] != "registry.ollama.ai":
            continue
        tag = rel_parts[-1]
        name_parts = list(rel_parts[1:-1])
        if name_parts and name_parts[0] == "library":
            name_parts = name_parts[1:]
        name = f"{'/'.join(name_parts)}:{tag}"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        layers = data.get("layers", [])
        model_layers = [
            layer for layer in layers if str(layer.get("mediaType", "")).endswith(".model")
        ]
        candidates = model_layers
        if not candidates:
            candidates = [
                layer
                for layer in layers
                if layer.get("digest")
                and isinstance(layer.get("size"), int)
                and layer["size"] >= 102_400  # skip configs/templates/adapters for models
                and not str(layer.get("mediaType", "")).startswith("application/vnd.ollama.image.")
            ]
        pick: dict | None = None
        for layer in candidates:
            blob = blobs / _digest_filename(str(layer["digest"]))
            if blob.exists() and (
                pick is None or blob.stat().st_size > pick["blob"].stat().st_size
            ):
                pick = {"layer": layer, "blob": blob}
        if pick is None:
            continue
        info = OllamaModelInfo(
            name=name,
            tag=tag,
            size_bytes=pick["blob"].stat().st_size,
            blob=pick["blob"],
            media_type=str(pick["layer"].get("mediaType", "")),
        )
        # prefer the largest artifact per name:tag
        if name not in found or info.size_bytes > found[name].size_bytes:
            found[name] = info
    return sorted(found.values(), key=lambda i: i.name)


def dest_path(models_dir: str | Path, info: OllamaModelInfo) -> Path:
    safe = info.name.replace("/", "_").replace(":", "-")
    return Path(models_dir) / f"{safe}.gguf"


def migrate(
    info: OllamaModelInfo,
    models_dir: str | Path,
    *,
    mode: str = "link",
    overwrite: bool = False,
) -> tuple[Path, str]:
    """Land one Ollama model into ``models_dir``. Returns (path, method)."""
    if mode not in {"link", "copy"}:
        raise ValueError("mode must be link|copy")
    target = dest_path(models_dir, info)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        same_size = target.stat().st_size == info.blob.stat().st_size
        if same_size and not overwrite:
            return target, "exists"
        if not overwrite:
            raise ResourceUnavailable(f"{target} exists with different size; pass overwrite")
        target.unlink()
    if mode == "link":
        try:
            os.link(info.blob, target)
            return target, "hardlink"
        except OSError:
            pass  # cross-volume or OneDrive placeholder — fall through to copy
    shutil.copy2(info.blob, target)
    return target, "copy"
