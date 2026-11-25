"""The inference engine contract (SPEC §9, §56.13).

Backends: llama.cpp (primary #1), Ollama (compatibility #2), mock (tests).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

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
    #: optional base64-encoded images for vision/multimodal models
    images: list[str] = Field(default_factory=list)
    #: optional JSON Schema for constrained structured generation. Engines
    #: that support it (e.g. llama-server ``response_format`` json_schema)
    #: constrain sampling to the schema; engines that do not ignore it.
    #: This NEVER replaces downstream validation — the brain still parses
    #: and validates all returned text fail-closed.
    response_schema: dict[str, Any] | None = Field(default=None)


class Cost(Contract):
    """Estimated cost in USD, or an EXPLICIT unknown (SPEC §10)."""

    usd: float | None = None
    basis: Literal["provider_reported", "estimated_from_catalog", "unknown"] = "unknown"


class AttemptRecord(Contract):
    model_id: str
    outcome: str  # "ok" or a typed error kind
    latency_s: float
    detail: str = ""  # redacted, bounded


class InferenceTrace(Contract):
    """Attribution record for one logical inference request (SPEC §10).

    Never contains credentials or prompt text."""

    trace_id: str
    outcome: Literal["success", "failure"]
    requested_model_id: str
    #: provider-qualified id that produced the result (None on failure)
    model_id: str | None = None
    provider: str | None = None
    #: what the provider reports it actually served (e.g. a router's pick)
    served_model: str | None = None
    latency_s: float
    attempts: list[AttemptRecord] = Field(default_factory=list)
    retries: int = 0
    fallbacks: int = 0
    fallback_reasons: list[str] = Field(default_factory=list)
    validation: Literal["not_requested", "passed", "failed"] = "not_requested"
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Cost = Field(default_factory=Cost)
    free_only: bool = False
    catalog_retrieved_at: datetime | None = None
    #: raw declared price fields of the model that served the request
    declared_pricing: dict[str, str] = Field(default_factory=dict)
    context_window: int | None = None
    estimated_input_tokens: int | None = None
    error_kind: str | None = None


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
    #: set by cloud engines only; local engines leave it None
    trace: InferenceTrace | None = None


def ttft_from_decode(latency_s: float, decode_s: float | None, tokens: int | None) -> float | None:
    """Time to first token for a NON-streaming call: wall-clock latency minus
    the server-reported decode time spent after the first token. None when the
    server reports no decode timing — never the total latency (SPEC §12)."""
    if decode_s is None or not tokens or tokens < 1:
        return None
    return max(latency_s - decode_s * (tokens - 1) / tokens, 0.0)


class EngineHealth(Contract):
    status: ModelHealth
    detail: str = ""
    models: list[str] = Field(default_factory=list)
    #: model id -> context the engine reports as SERVED (e.g. llama-server's
    #: /v1/models meta.n_ctx). Measured serving truth, never a filename
    #: proxy (SPEC §12); registry records consume it when present.
    context_windows: dict[str, int] = Field(default_factory=dict)


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
