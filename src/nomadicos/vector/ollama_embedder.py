"""Ollama-backed local embedder (Phase 9 semantic upgrade, I1-safe).

Uses the LOCAL Ollama server (no external API — BP §345 holds). Recommended
model: nomic-embed-text (274 MB, 768-d). Any installed Ollama model works.
"""
from __future__ import annotations

import httpx

from nomadicos.vector.embeddings import Embedder, EmbedderInfo


class OllamaEmbedder(Embedder):
    def __init__(
        self,
        *,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        dimensions: int = 768,
        timeout_s: float = 60.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._dimensions = dimensions
        self._timeout_s = timeout_s
        self._info = EmbedderInfo(
            model_id=f"ollama/{model}", version="1.0.0", dimensions=dimensions
        )

    @property
    def info(self) -> EmbedderInfo:
        return self._info

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(
                f"{self._base_url}/api/embeddings",
                json={"model": self._model, "prompt": text[:2000]},
            )
            response.raise_for_status()
            return response.json()["embedding"]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity without dependencies; 0.0 on length/dimension mismatch."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = ["OllamaEmbedder", "cosine"]
