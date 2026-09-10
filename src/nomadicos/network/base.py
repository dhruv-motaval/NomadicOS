"""NetworkTransport contract (BP §99): request minimization + provenance metadata.

The transport layer is deliberately dumb: policy checks happen in the Network
Gateway (Phase 7), never inside the transport. Responses are size-bounded
(BP §87).
"""

import time
from abc import ABC, abstractmethod
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

MAX_BODY_BYTES = 5 * 1024 * 1024


class NetworkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "HEAD"]  # v0.1: information retrieval only (BP §268)
    url: str = Field(min_length=8, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_body_bytes: int = Field(default=MAX_BODY_BYTES, gt=0)


class NetworkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = Field(default=b"", repr=False)
    url: str
    elapsed_ms: float = Field(ge=0)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class NetworkTransport(ABC):
    """Pluggable HTTP transport. The default `HttpxTransport` is implemented in
    Phase 7 together with the Network Gateway; tests use `FakeNetworkTransport`."""

    @abstractmethod
    async def request(self, request: NetworkRequest) -> NetworkResponse: ...


class HttpxTransport(NetworkTransport):
    """Thin httpx-backed transport; one attempt, no retries (router owns fallback)."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def request(self, request: NetworkRequest) -> NetworkResponse:
        import asyncio

        started = time.monotonic()
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        response = await asyncio.wait_for(
            self._client.request(
                request.method,
                request.url,
                headers=request.headers,
                timeout=request.timeout_seconds,
            ),
            timeout=request.timeout_seconds,
        )
        body = response.content[: request.max_body_bytes]
        return NetworkResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=body,
            url=str(response.url),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


__all__ = ["HttpxTransport", "NetworkRequest", "NetworkResponse", "NetworkTransport"]
