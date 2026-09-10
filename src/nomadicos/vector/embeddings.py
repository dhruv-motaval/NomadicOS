"""Embedding interface (BP §345-347): local-only, configurable, versioned.

The canonical embedding model is NOT locked before benchmarking (ADR-0003);
every index records model/version/dimensions/metric (BP §107, §311).
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict


class EmbedderInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str
    version: str
    dimensions: int


class Embedder(ABC):
    """Local embedding model contract. No remote embedding API (BP §345)."""

    @property
    @abstractmethod
    def info(self) -> EmbedderInfo: ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


class FakeEmbedder(Embedder):
    """Deterministic hash-based embedder for CI (384-d baseline, ADR-0003).

    Real local models (sentence-transformers class) implement the same
    Embedder interface — chosen after the retrieval benchmark (ADR-0003).
    """

    def __init__(self, dimensions: int = 384, model_id: str = "fake/hash-embed") -> None:
        self._dimensions = dimensions
        self._info = EmbedderInfo(
            model_id=model_id, version="1.0.0", dimensions=dimensions
        )

    @property
    def info(self) -> EmbedderInfo:
        return self._info

    async def embed(self, text: str) -> list[float]:
        import hashlib
        import math

        # Deterministic bag-of-hashes: token hashes seed evenly spread buckets.
        buckets = [0.0] * self._dimensions
        tokens = text.lower().split()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            buckets[index] += 1.0
        norm = math.sqrt(sum(x * x for x in buckets))
        if norm == 0:
            buckets[0] = 1.0
            norm = 1.0
        return [x / norm for x in buckets]


__all__ = ["Embedder", "EmbedderInfo", "FakeEmbedder"]
