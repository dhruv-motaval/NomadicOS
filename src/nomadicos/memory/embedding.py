"""Embedding abstraction for semantic memory retrieval (Phase 5, SPEC §52G).

Provides a replaceable embedding model interface. The default implementation
uses Ollama's `/api/embeddings` endpoint with ``nomic-embed-text`` — a local,
open-weight embedding model that runs entirely on the owner's machine.

Design principles (mirroring the rest of NomadicOS):

- **Models propose, NomadicOS authorizes**: embeddings are pure data
  transformations; they never authorize, execute, or change any state.
- **Fail-safe**: if Ollama is unavailable, the model is missing, or the
  embedding dimension mismatches the stored index, the embedding layer raises
  :class:`EmbeddingError` and callers fall back to lexical-only retrieval.
  Memory is never lost because of an embedding problem.
- **No cloud APIs**: only local Ollama is supported; no cloud embedding
  providers are ever contacted.
- **Embedding model/version metadata** is stored with every vector so that
  index rebuilds can be triggered when the model changes.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import httpx

EMBEDDING_DIM = 768  # nomic-embed-text produces 768-dimensional vectors


class EmbeddingError(Exception):
    """Raised when an embedding cannot be produced.

    Callers treat this as "semantic retrieval unavailable" and fall back to
    lexical-only retrieval. Never propagated as a task failure.
    """


@runtime_checkable
class EmbeddingModel(Protocol):
    """A replaceable embedding model.

    Implementations produce dense vector embeddings for text. The model is
    purely a data transformation: it never authorizes, executes, or changes
    any NomadicOS state.
    """

    model_id: str
    """Human-readable model identifier (e.g. ``nomic-embed-text``)."""

    embedding_dim: int
    """Dimensionality of the embedding vectors this model produces."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in one call.

        Returns one embedding vector per input text, in the same order.
        Raises :class:`EmbeddingError` if any embedding cannot be produced.
        """
        raise NotImplementedError

    async def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Convenience wrapper around :meth:`embed`."""
        vectors = await self.embed([text])
        return vectors[0]


class OllamaEmbeddingModel:
    """Ollama-based embedding model using ``nomic-embed-text``.

    Calls Ollama's ``/api/embeddings`` endpoint on the owner's machine.
    All requests are local HTTP; no cloud API is ever contacted.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        default_timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_id = model
        self.embedding_dim = EMBEDDING_DIM
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=default_timeout_s)
        self._default_timeout_s = default_timeout_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append(await self._embed_one(text))
        return vectors

    async def _embed_one(self, text: str) -> list[float]:
        payload = {
            "model": self.model_id,
            "prompt": text,
        }
        try:
            resp = await self._client.post("/api/embeddings", json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"ollama embedding request failed: {exc}") from exc
        if resp.status_code == 404:
            raise EmbeddingError(
                f"embedding model {self.model_id!r} not found on ollama; "
                f"pull it first: ollama pull {self.model_id}"
            )
        if resp.status_code >= 400:
            raise EmbeddingError(f"ollama embedding error {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        vector = body.get("embedding")
        if not isinstance(vector, list) or not all(isinstance(v, (int, float)) for v in vector):
            raise EmbeddingError(
                f"ollama embedding returned invalid vector for model {self.model_id!r}"
            )
        if len(vector) != self.embedding_dim:
            raise EmbeddingError(
                f"ollama embedding dimension {len(vector)} != expected "
                f"{self.embedding_dim} for model {self.model_id!r}; "
                f"the model or index may have changed — rebuild the index"
            )
        return [float(v) for v in vector]

    async def embed_one(self, text: str) -> list[float]:
        return await self._embed_one(text)

    async def health(self) -> bool:
        """Return True if the embedding model is available and healthy."""
        try:
            resp = await self._client.post(
                "/api/embeddings", json={"model": self.model_id, "prompt": "health check"}
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


def embedding_dim_for_model(model_id: str) -> int:
    """Return the embedding dimension for a known model id.

    Unknown models default to the nomic-embed-text dimension (768).
    """
    _KNOWN_DIMS: dict[str, int] = {
        "nomic-embed-text": 768,
        "nomic-embed-text-v1.5": 768,
        "all-minilm": 384,
        "all-mpnet-base-v2": 768,
    }
    return _KNOWN_DIMS.get(model_id, EMBEDDING_DIM)


def stable_embedding_id(model_id: str, embedding_dim: int, text: str) -> str:
    """Deterministic content-addressed id for an embedding.

    Format: ``emb-<sha16(model_id|dim|text)>``. Same text + same model
    always yields the same id, which makes embedding storage idempotent.
    """
    digest = hashlib.sha256(f"{model_id}|{embedding_dim}|{text}".encode()).hexdigest()[:16]
    return f"emb-{digest}"
