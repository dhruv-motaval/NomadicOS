"""Deterministic mock engine for tests and offline development (SPEC §10)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from nomadicos.contracts.model import ModelHealth
from nomadicos.inference.base import (
    EngineHealth,
    GenerationRequest,
    GenerationResponse,
    InferenceEngine,
)
from nomadicos.kernel.errors import EngineUnavailable, ModelError


class MockEngine(InferenceEngine):
    """Scripted responses matched by substring against the last user message.

    Falls back to ``default_response``; if a response is registered as an
    Exception it is raised (to exercise failure paths).
    """

    engine_id = "mock"

    def __init__(self, default_response: str = "", unavailable: bool = False) -> None:
        self.rules: list[tuple[str, str | list[str] | Exception]] = []
        self.default_response = default_response
        self.unavailable = unavailable
        self.known_models: set[str] = set()
        self.fail_models: set[str] = set()
        self.calls: list[GenerationRequest] = []

    def script(self, contains: str, response: str | list[str] | Exception) -> None:
        self.rules.append((contains, response))

    def fail_for(self, *model_ids: str) -> None:
        """Mark models as failing (escalation tests)."""
        self.fail_models.update(model_ids)

    def register_model(self, model_id: str) -> None:
        self.known_models.add(model_id)

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        if self.unavailable:
            raise EngineUnavailable("mock engine unavailable")
        if request.model_id in self.fail_models:
            raise ModelError(f"model {request.model_id!r} is failing")
        self.calls.append(request)
        prompt = request.messages[-1].content
        for needle, response in self.rules:
            if needle in prompt:
                if isinstance(response, list):
                    reply = response.pop(0) if len(response) > 1 else response[0]
                    return GenerationResponse(
                        engine_id=self.engine_id, model_id=request.model_id, text=reply
                    )
                if isinstance(response, Exception):
                    raise response
                return GenerationResponse(
                    engine_id=self.engine_id, model_id=request.model_id, text=response
                )
        return GenerationResponse(
            engine_id=self.engine_id,
            model_id=request.model_id,
            text=self.default_response,
        )

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationResponse]:
        full = await self.generate(request)
        words = full.text.split(" ")
        for i, word in enumerate(words):
            await asyncio.sleep(0)
            yield GenerationResponse(
                engine_id=self.engine_id,
                model_id=full.model_id,
                text=word + (" " if i < len(words) - 1 else ""),
                is_delta=True,
            )

    async def list_models(self) -> list[str]:
        return sorted(self.known_models)

    async def health(self) -> EngineHealth:
        models = await self.list_models()
        if self.unavailable:
            return EngineHealth(
                status=ModelHealth.UNHEALTHY, detail="mock unavailable", models=models
            )
        return EngineHealth(status=ModelHealth.HEALTHY, detail="mock ready", models=models)
