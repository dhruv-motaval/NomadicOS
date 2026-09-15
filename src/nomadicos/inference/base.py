"""The inference engine contract (SPEC §9, §56.13).

Backends: llama.cpp (primary #1), Ollama (compatibility #2), mock (tests).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import Field

from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import ModelHealth

Role = Literal["system", "user", "assistant"]


class ChatMessage(Contract):
    role: Role
    content: str


class GenerationRequest(Contract):
    model_id: str
    messages: list[ChatMessage] = Field(min_length=1)
    #: low by default for worker reliability; callers may raise
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    timeout_s: float | None = Field(default=None, gt=0)


class GenerationResponse(Contract):
    engine_id: str
    model_id: str
    text: str
    #: cumulative for stream chunks after the delta is emitted
    is_delta: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float | None = None
    time_to_first_token_s: float | None = None
    #: measurements, not claims (SPEC §12)
    tokens_per_second: float | None = None


class EngineHealth(Contract):
    status: ModelHealth
    detail: str = ""
    models: list[str] = Field(default_factory=list)


class InferenceEngine(ABC):
    """Serving contract satisfied by llama.cpp, Ollama, and Mock bricks."""

    engine_id: str = "?"

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResponse: ...

    @abstractmethod
    def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationResponse]: ...

    @abstractmethod
    async def list_models(self) -> list[str]: ...

    @abstractmethod
    async def health(self) -> EngineHealth: ...

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        return None
