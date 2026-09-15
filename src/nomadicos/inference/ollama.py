"""Ollama compatibility backend (SPEC §9, priority #2).

Local-only HTTP API on the owner's machine. Also imports model files dropped
into ``models/`` via ``/api/create`` so an owner-placed GGUF becomes a usable
Ollama model without leaving the machine.
"""

from __future__ import annotations

import json
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


class OllamaEngine(InferenceEngine):
    engine_id = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_timeout_s: float = 600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=default_timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        payload = self._chat_payload(request, stream=False)
        started = time.monotonic()
        try:
            resp = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"ollama request failed: {exc}") from exc
        if resp.status_code == 404:
            raise ModelError(f"unknown model {request.model_id!r} on ollama")
        if resp.status_code >= 400:
            raise ModelError(f"ollama error {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        message = body.get("message") or {}
        latency = time.monotonic() - started
        out_tokens = body.get("eval_count")
        return GenerationResponse(
            engine_id=self.engine_id,
            model_id=request.model_id,
            text=str(message.get("content", "")),
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=out_tokens,
            latency_s=latency,
            time_to_first_token_s=latency,
            tokens_per_second=self._tps(body),
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationResponse]:
        payload = self._chat_payload(request, stream=True)
        started = time.monotonic()
        ttft: float | None = None
        produced = 0
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ModelError(f"ollama stream error {resp.status_code}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    delta = str((event.get("message") or {}).get("content", ""))
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
                    if event.get("done"):
                        yield GenerationResponse(
                            engine_id=self.engine_id,
                            model_id=request.model_id,
                            text="",
                            output_tokens=event.get("eval_count") or produced,
                            latency_s=time.monotonic() - started,
                            time_to_first_token_s=ttft,
                            tokens_per_second=self._tps(event),
                        )
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"ollama stream failed: {exc}") from exc

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"ollama list_models failed: {exc}") from exc
        return [m["name"] for m in resp.json().get("models", []) if "name" in m]

    async def health(self) -> EngineHealth:
        try:
            resp = await self._client.get("/api/version", timeout=5.0)
            if resp.status_code != 200:
                return EngineHealth(
                    status=ModelHealth.UNHEALTHY, detail=f"status {resp.status_code}"
                )
            models = await self.list_models()
            return EngineHealth(
                status=ModelHealth.HEALTHY, detail=resp.json().get("version", ""), models=models
            )
        except httpx.HTTPError as exc:
            return EngineHealth(status=ModelHealth.UNHEALTHY, detail=str(exc))

    async def import_model(self, name: str, path: Path, quantize: bool = False) -> str:
        """Register a model file from ``models/`` as an Ollama model."""
        if not path.exists():
            raise ModelError(f"model file not found: {path}")
        modelfile = f"FROM {path.as_posix()}"
        if quantize:
            modelfile += "\nTEMPLATE {{ .Prompt }}"
        try:
            async with self._client.stream(
                "POST", "/api/create", json={"name": name, "modelfile": modelfile}
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ModelError(f"ollama create failed {resp.status_code}: {resp.text[:200]}")
                async for line in resp.aiter_lines():
                    if line.strip():
                        event = json.loads(line)
                        if "success" in str(event.get("status", "")).lower() or event.get("done"):
                            break
        except httpx.HTTPError as exc:
            raise EngineUnavailable(f"ollama create failed: {exc}") from exc
        return name

    @staticmethod
    def _tps(body: dict) -> float | None:
        count = body.get("eval_count")
        ns = body.get("eval_duration")
        if count and ns:
            return round(count / (ns / 1e9), 2)
        return None

    @staticmethod
    def _chat_payload(request: GenerationRequest, stream: bool) -> dict:
        options: dict[str, float | int] = {"temperature": request.temperature}
        if request.max_tokens is not None:
            options["num_predict"] = request.max_tokens
        return {
            "model": request.model_id,
            "messages": [m.model_dump() for m in request.messages],
            "stream": stream,
            "options": options,
        }
