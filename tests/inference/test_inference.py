"""Inference brick contract tests — engines are swappable, local-first (§9-10)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from nomadicos.contracts.model import ModelHealth
from nomadicos.inference import (
    ChatMessage,
    GenerationRequest,
    InferenceEngine,
    LlamaCppEngine,
    LlamaServerLauncher,
    MockEngine,
    OllamaEngine,
)
from nomadicos.kernel.config import AppConfig, InferenceConfig
from nomadicos.kernel.errors import EngineUnavailable, ModelError


def req(prompt: str = "hello", model: str = "m1") -> GenerationRequest:
    return GenerationRequest(model_id=model, messages=[ChatMessage(role="user", content=prompt)])


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


# ---------------------------------------------------------------- Mock ------


async def test_mock_is_engine_contract() -> None:
    engine: InferenceEngine = MockEngine()
    assert engine.engine_id == "mock"
    health = await engine.health()
    assert health.status is ModelHealth.HEALTHY


async def test_mock_scripted_and_recorded() -> None:
    engine = MockEngine()
    engine.script("rename", '{"tool": "terminal"}')
    out = await engine.generate(req("please rename the file"))
    assert out.text == '{"tool": "terminal"}'
    assert len(engine.calls) == 1
    default = await engine.generate(req("unrelated"))
    assert default.text == ""


async def test_mock_stream_yields_deltas() -> None:
    engine = MockEngine()
    engine.script("x", "alpha beta gamma")
    chunks = [c async for c in engine.stream(req("say x"))]
    assert "".join(c.text for c in chunks) == "alpha beta gamma"
    assert all(c.is_delta for c in chunks)


async def test_mock_unavailable_fails_loud() -> None:
    engine = MockEngine(unavailable=True)
    with pytest.raises(EngineUnavailable):
        await engine.generate(req())
    assert (await engine.health()).status is ModelHealth.UNHEALTHY


# -------------------------------------------------------------- Ollama ------


async def test_ollama_generate_chat_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["stream"] is False
        assert body["model"] == "ornith"
        return httpx.Response(
            200,
            json={
                "model": "ornith",
                "message": {"role": "assistant", "content": "done"},
                "done": True,
                "prompt_eval_count": 12,
                "eval_count": 3,
                "eval_duration": 1_000_000_000,
            },
        )

    engine = OllamaEngine(client=transport(handler))
    out = await engine.generate(req("hi", model="ornith"))
    assert out.text == "done"
    assert out.input_tokens == 12
    assert out.output_tokens == 3
    assert out.tokens_per_second == 3.0
    assert out.engine_id == "ollama"


async def test_ollama_stream_parses_ndjson() -> None:
    lines = (
        json.dumps({"message": {"content": "Hel"}, "done": False})
        + "\n"
        + json.dumps({"message": {"content": "lo"}, "done": False})
        + "\n"
        + json.dumps({"message": {"content": ""}, "done": True, "eval_count": 2})
        + "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=lines.encode())

    engine = OllamaEngine(client=transport(handler))
    parts = [c async for c in engine.stream(req())]
    assert "".join(p.text for p in parts) == "Hello"
    assert parts[0].is_delta and parts[1].is_delta
    assert parts[-1].is_delta is False
    assert parts[-1].output_tokens == 2
    assert parts[-1].time_to_first_token_s is not None


async def test_ollama_list_health_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:14b"}, {"name": "x"}]})
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.9"})
        return httpx.Response(404, json={"error": "model not found"})

    engine = OllamaEngine(client=transport(handler))
    assert await engine.list_models() == ["qwen3:14b", "x"]
    health = await engine.health()
    assert health.status is ModelHealth.HEALTHY
    assert "qwen3:14b" in health.models
    with pytest.raises(ModelError):
        await engine.generate(req())


async def test_ollama_connection_refused_is_engine_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    engine = OllamaEngine(client=transport(handler))
    with pytest.raises(EngineUnavailable):
        await engine.generate(req())
    assert (await engine.health()).status is ModelHealth.UNHEALTHY


async def test_ollama_import_model(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["name"] = body["name"]
        seen["modelfile"] = body["modelfile"]
        return httpx.Response(
            200,
            content=(json.dumps({"status": "success"}) + "\n").encode(),
        )

    gguf = tmp_path / "ornith.Q4_K_M.gguf"
    gguf.write_bytes(b"fake")
    engine = OllamaEngine(client=transport(handler))
    name = await engine.import_model("ornith", gguf)
    assert name == "ornith"
    assert seen["modelfile"].startswith("FROM ")
    with pytest.raises(ModelError):
        await engine.import_model("missing", tmp_path / "nope.gguf")


# ---------------------------------------------------------- llama.cpp -------


async def test_llamacpp_generate_and_tps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello back"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
            },
        )

    engine = LlamaCppEngine(client=transport(handler))
    out = await engine.generate(req())
    assert out.text == "hello back"
    assert out.engine_id == "llamacpp"
    assert out.tokens_per_second is not None


async def test_llamacpp_stream_sse() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Foo"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Bar"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse.encode())

    engine = LlamaCppEngine(client=transport(handler))
    parts = [c async for c in engine.stream(req())]
    assert "".join(p.text for p in parts) == "FooBar"


async def test_llamacpp_down_reports_unhealthy_and_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("llama-server not running", request=request)

    engine = LlamaCppEngine(client=transport(handler))
    health = await engine.health()
    assert health.status is ModelHealth.UNHEALTHY
    with pytest.raises(EngineUnavailable):
        await engine.generate(req())
    with pytest.raises(EngineUnavailable):
        await engine.list_models()


def test_llama_launcher_validates_before_spawning(tmp_path: Path) -> None:
    launcher = LlamaServerLauncher(binary="definitely-not-llama-server-xyz")
    assert launcher.is_running() is False
    with pytest.raises(ModelError):
        launcher.start(tmp_path / "absent.gguf")
    gguf = tmp_path / "ornith.Q4.gguf"
    gguf.write_bytes(b"x")
    with pytest.raises(EngineUnavailable):
        launcher.start(gguf)


# ------------------------------------------- portable binary discovery ------
#
# Post-release hardening: llama-server must be configurable without a
# machine-specific absolute path. The default resolves from PATH; an
# explicit absolute path keeps working and wins over discovery.


def test_llama_launcher_accepts_explicit_absolute_binary() -> None:
    """An explicitly configured, existing executable is accepted as-is."""
    # sys.executable is always an existing, executable absolute path.
    assert LlamaServerLauncher.available(sys.executable)
    launcher = LlamaServerLauncher(binary=sys.executable)
    assert launcher.binary == sys.executable
    assert launcher.is_running() is False


def test_llama_launcher_discovers_binary_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare binary name is discovered through PATH (portable default)."""
    bin_dir = tmp_path / "llama-bin"
    bin_dir.mkdir()
    exe = bin_dir / ("fake-llama-server.cmd" if os.name == "nt" else "fake-llama-server")
    exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n")
    if os.name != "nt":
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    assert LlamaServerLauncher.available("fake-llama-server")
    launcher = LlamaServerLauncher(binary="fake-llama-server")
    assert not launcher.is_running()


def test_llama_launcher_unavailable_binary_is_explicit(tmp_path: Path) -> None:
    """A nonexistent binary reports unavailable instead of silently picking
    an unintended executable; a missing model file is reported by name."""
    launcher = LlamaServerLauncher(binary="definitely-not-llama-server-xyz")
    assert not LlamaServerLauncher.available(launcher.binary)
    gguf = tmp_path / "present.gguf"
    gguf.write_bytes(b"x")
    with pytest.raises(EngineUnavailable):
        launcher.start(gguf)


def test_config_fallback_engine_policy_preserved() -> None:
    """llama.cpp stays the default engine with Ollama as the declared
    fallback; the two engines must differ (fail closed, never a loop)."""
    cfg = AppConfig()
    assert cfg.inference.default_engine == "llamacpp"
    assert cfg.inference.fallback_engine == "ollama"
    assert cfg.inference.ollama_base_url == "http://localhost:11434"
    with pytest.raises(Exception, match="default and fallback inference engines must differ"):
        AppConfig(inference=InferenceConfig(default_engine="ollama", fallback_engine="ollama"))


async def test_engines_are_interchangeable() -> None:
    """SPEC §3: all bricks satisfy one contract (Lego interchange test)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "same"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "same"}}], "usage": {}}
        )

    engines: list[InferenceEngine] = [
        MockEngine(default_response="same"),
        LlamaCppEngine(client=transport(handler)),
        OllamaEngine(client=transport(handler)),
    ]
    texts = []
    for engine in engines:
        out = await engine.generate(req("prompt"))
        texts.append(out.text)
    assert texts == ["same", "same", "same"]
