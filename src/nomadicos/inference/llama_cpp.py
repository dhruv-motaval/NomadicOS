"""llama.cpp engine — primary local inference backend (#1, owner directive 2026-09-15).

NomadicOS speaks to a local ``llama-server`` process over its
OpenAI-compatible HTTP surface (``/v1/chat/completions``, ``/v1/models``,
``/health``). Nothing from llama.cpp internals leaks into agents, security,
the executor, tools, memory, or verifiers — it stays behind this contract
and is replaceable by Ollama (#2) or Mock (tests) by configuration (SPEC §3, §9).

A ``LlamaServerLauncher`` can start/stop ``llama-server`` for a GGUF placed in
``models/`` when the owner has the binary available; otherwise the engine
reports health honestly and the router falls back by configuration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from nomadicos.contracts.model import ModelHealth
from nomadicos.inference.base import (
    EngineHealth,
    GenerationRequest,
    GenerationResponse,
    InferenceEngine,
)
from nomadicos.kernel.errors import EngineUnavailable, ModelError


class LlamaServerLauncher:
    """Owns one local llama-server subprocess (safety: process cleanup §6)."""

    def __init__(
        self,
        binary: str = "llama-server",
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        extra_args: list[str] | None = None,
    ) -> None:
        self.binary = binary
        self.host = host
        self.port = port
        self.extra_args = list(extra_args or [])
        self._proc: subprocess.Popen[bytes] | None = None

    @staticmethod
    def available(binary: str = "llama-server") -> bool:
        return shutil.which(binary) is not None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, model_path: str | Path, context_size: int = 8192) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise ModelError("llama-server already running")
        path = Path(model_path)
        if not path.exists():
            raise ModelError(f"model file not found: {path}")
        if not self.available(self.binary):
            raise EngineUnavailable(f"{self.binary!r} not on PATH")
        self._proc = subprocess.Popen(  # noqa: S603
            [
                self.binary,
                "-m",
                str(path),
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--ctx-size",
                str(context_size),
                *self.extra_args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
            self._proc = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class LlamaCppEngine(InferenceEngine):
    engine_id = "llamacpp"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8080",
        default_timeout_s: float = 600.0,
        client: httpx.AsyncClient | None = None,
        launcher: LlamaServerLauncher | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=default_timeout_s)
        self._launcher = launcher

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._launcher is not None:
            self._launcher.stop()

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._payload(request, stream=False)
        started = time.monotonic()
        try:
            resp = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"llama-server request failed: {exc}") from exc
        if resp.status_code == 404:
            raise ModelError(f"llama-server unknown model {request.model_id!r}")
        if resp.status_code >= 400:
            raise ModelError(f"llama-server error {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        latency = max(time.monotonic() - started, 1e-6)
        usage = body.get("usage") or {}
        text = self._first_text(body)
        return GenerationResponse(
            engine_id=self.engine_id,
            model_id=request.model_id,
            text=text,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_s=latency,
            time_to_first_token_s=latency,
            tokens_per_second=self._tps(usage, latency),
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationResponse]:
        payload = self._payload(request, stream=True)
        started = time.monotonic()
        ttft: float | None = None
        produced = 0
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ModelError(f"llama-server stream error {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[len("data:") :].strip()
                    if chunk == "[DONE]":
                        break
                    event = json.loads(chunk)
                    delta = ""
                    choices = event.get("choices") or []
                    if choices:
                        delta = str((choices[0].get("delta") or {}).get("content", ""))
                    if delta:
                        if ttft is None:
                            ttft = time.monotonic() - started
                        produced += 1
                        yield GenerationResponse(
                            engine_id=self.engine_id,
                            model_id=request.model_id,
                            text=delta,
                            is_delta=True,
                            time_to_first_token_s=ttft,
                        )
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"llama-server stream failed: {exc}") from exc
        yield GenerationResponse(
            engine_id=self.engine_id,
            model_id=request.model_id,
            text="",
            output_tokens=produced,
            latency_s=time.monotonic() - started,
            time_to_first_token_s=ttft,
        )

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get("/v1/models")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"llama-server list_models failed: {exc}") from exc
        return [m["id"] for m in resp.json().get("data", []) if "id" in m]

    async def health(self) -> EngineHealth:
        try:
            resp = await self._client.get("/health", timeout=5.0)
            if resp.status_code != 200:
                return EngineHealth(
                    status=ModelHealth.UNHEALTHY, detail=f"status {resp.status_code}"
                )
            models = await self.list_models()
            return EngineHealth(
                status=ModelHealth.HEALTHY, detail="llama-server ready", models=models
            )
        except httpx.HTTPError as exc:
            return EngineHealth(status=ModelHealth.UNHEALTHY, detail=str(exc))

    @staticmethod
    def _first_text(body: dict) -> str:
        choices = body.get("choices") or []
        if not choices:
            raise ModelError("llama-server returned no choices")
        message = choices[0].get("message") or {}
        return str(message.get("content", ""))

    @staticmethod
    def _tps(usage: dict, latency: float) -> float | None:
        out = usage.get("completion_tokens")
        if out and latency > 0:
            return round(out / latency, 2)
        return None

    @staticmethod
    def _payload(request: GenerationRequest, stream: bool) -> dict:
        payload: dict = {
            "model": request.model_id,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload
