"""models/ is the owner's model inventory; Ollama cannot widen it (SPEC §9-10, §12).

Ollama keeps its own store, so a model the owner removed from ``models/``
would otherwise come back as a HEALTHY, routable record on every sync, and
Ollama's cloud models would put task context on ollama.com. The fake
server below mirrors a real ``/api/tags`` listing, cloud entry included.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from nomadicos.inference.ollama import OllamaEngine, is_cloud_model_name
from nomadicos.kernel.config import ModelRoles
from nomadicos.kernel.events import EventLogger
from nomadicos.registry import ModelRegistry, RegistryBuilder
from nomadicos.registry.ollama_store import inventory_filename

TAGS = {
    "models": [
        {"name": "glm-5.3-flash:cloud", "remote_host": "https://ollama.com"},
        {"name": "gpt-oss:120b-cloud"},  # older builds omit the remote fields
        {"name": "ornith:latest"},
        {"name": "llama3.1:latest"},
        {"name": "qwen3:14b"},
        {"name": "qwen3-coder:30b-a3b-q4_K_M"},
        {"name": "nomad-oss:latest", "remote_model": ""},
    ]
}


def ollama() -> OllamaEngine:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    return OllamaEngine(client=client)


def owner_models(tmp_path: Path, *names: str) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    for name in names:
        (models / name).write_bytes(b"gguf")
    return models


async def test_engine_never_reports_cloud_models() -> None:
    names = await ollama().list_models()
    assert "glm-5.3-flash:cloud" not in names
    assert "gpt-oss:120b-cloud" not in names
    assert "ornith:latest" in names and "nomad-oss:latest" in names


def test_cloud_names_are_recognized() -> None:
    assert is_cloud_model_name("glm-5.3-flash:cloud")
    assert is_cloud_model_name("gpt-oss:120b-CLOUD")
    assert not is_cloud_model_name("ornith:latest")
    assert not is_cloud_model_name("cloudy-model:latest")


async def test_only_models_in_the_owner_inventory_are_registered(tmp_path: Path) -> None:
    models = owner_models(tmp_path, "ornith-latest.gguf", "Llama3.1-Latest.gguf", "notes.txt")
    registry = ModelRegistry(EventLogger())
    await RegistryBuilder(ModelRoles()).sync_from_engine(registry, ollama(), models_dir=models)
    assert {r.model_id for r in registry.all()} == {"ornith:latest", "llama3.1:latest"}


async def test_a_config_role_can_name_an_ollama_model_explicitly(tmp_path: Path) -> None:
    models = owner_models(tmp_path)
    roles = ModelRoles(critic="qwen3:14b", worker="glm-5.3-flash:cloud")
    registry = ModelRegistry(EventLogger())
    await RegistryBuilder(roles).sync_from_engine(registry, ollama(), models_dir=models)
    # the owner's explicit choice counts; a cloud model never does
    assert {r.model_id for r in registry.all()} == {"qwen3:14b"}


async def test_missing_models_dir_registers_no_ollama_model(tmp_path: Path) -> None:
    registry = ModelRegistry(EventLogger())
    await RegistryBuilder(ModelRoles()).sync_from_engine(
        registry, ollama(), models_dir=tmp_path / "absent"
    )
    assert registry.all() == []


def test_inventory_name_matches_the_sync_ollama_mapping() -> None:
    assert inventory_filename("ornith:latest") == "ornith-latest.gguf"
    assert inventory_filename("hf.co/org/model:Q4_K_M") == "hf.co_org_model-Q4_K_M.gguf"
