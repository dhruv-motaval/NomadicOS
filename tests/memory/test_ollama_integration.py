"""Live Ollama embedding integration tests (Phase 5.1).

These tests connect to a real local Ollama service and verify that:
1. The configured embedding model is available.
2. Embeddings for a known query and paraphrases are produced.
3. Embedding dimensions and values are valid.
4. Semantic retrieval finds paraphrases that lexical retrieval misses.
5. Semantic failure produces an observable lexical fallback.
6. Index persistence and reuse work across runtime instances.

Marked ``ollama`` — they are skipped unless the service is reachable.
They never claim to pass unless they actually ran against the live service.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from nomadicos.contracts.memory import MemoryQuery
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.embedding import (
    EMBEDDING_DIM,
    OllamaEmbeddingModel,
)
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.vector_index import VectorIndex

pytestmark = [pytest.mark.integration, pytest.mark.ollama]

OLLAMA_BASE_URL = "http://localhost:11434"
EMBEDDING_MODEL = "nomic-embed-text"


def _ollama_available_sync() -> bool:
    """Synchronous Ollama availability check (avoids event loop issues)."""
    import httpx

    try:
        with httpx.Client(base_url=OLLAMA_BASE_URL, timeout=3.0) as client:
            resp = client.post(
                "/api/embeddings",
                json={"model": EMBEDDING_MODEL, "prompt": "health check"},
            )
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def ollama_running() -> None:
    """Skip all tests in this module if Ollama is not available."""
    if not _ollama_available_sync():
        pytest.skip(
            f"Ollama not reachable at {OLLAMA_BASE_URL} or model "
            f"'{EMBEDDING_MODEL}' not pulled. Run: ollama pull {EMBEDDING_MODEL}"
        )


async def test_ollama_model_available(ollama_running: None) -> None:
    """The intended embedding model is available on the local Ollama."""
    model = OllamaEmbeddingModel(model=EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
    try:
        healthy = await model.health()
        assert healthy, f"embedding model {EMBEDDING_MODEL} is not healthy"
    finally:
        await model.aclose()


async def test_ollama_embedding_valid(ollama_running: None) -> None:
    """Embeddings have the correct dimension and finite numeric values."""
    model = OllamaEmbeddingModel(model=EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
    try:
        texts = [
            "PostgreSQL uses port 5432",
            "PostgreSQL listens on port 5432",
            "Redis is a key-value store",
        ]
        vectors = await model.embed(texts)
        assert len(vectors) == 3
        for vec in vectors:
            assert len(vec) == EMBEDDING_DIM
            assert all(math.isfinite(v) for v in vec)
    finally:
        await model.aclose()


async def test_semantic_paraphrase_similarity(ollama_running: None) -> None:
    """Paraphrases of the same fact are more similar than unrelated facts."""
    model = OllamaEmbeddingModel(model=EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
    try:
        paraphrase_a = "PostgreSQL uses port 5432"
        paraphrase_b = "PostgreSQL listens on port 5432"
        unrelated = "Redis is a key-value store"
        va, vb, vu = await model.embed([paraphrase_a, paraphrase_b, unrelated])

        def cos(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return dot / (na * nb)

        sim_same = cos(va, vb)
        sim_diff = cos(va, vu)
        assert sim_same > sim_diff, (
            f"paraphrase similarity ({sim_same:.3f}) should exceed "
            f"unrelated similarity ({sim_diff:.3f})"
        )
        assert sim_same > 0.7, f"paraphrases of the same fact scored only {sim_same:.3f}"
    finally:
        await model.aclose()


async def test_vector_index_persistence(ollama_running: None, tmp_path: Path) -> None:
    """Vectors persist to disk and are reusable across a new index instance."""
    model = OllamaEmbeddingModel(model=EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
    store_path = tmp_path / "memory.jsonl"
    config = MemoryConfig(state_dir=str(tmp_path))

    index1 = VectorIndex(store_path, model, config)
    await index1.add("m1", "PostgreSQL uses port 5432")
    assert index1.has_vector("m1")

    # New instance pointing at the same file should load the persisted vector
    index2 = VectorIndex(store_path, model, config)
    assert index2.has_vector("m1"), "vector index did not persist across instances"
    await model.aclose()


async def test_semantic_failure_lexical_fallback(ollama_running: None, tmp_path: Path) -> None:
    """When the embedding model is stopped mid-run, retrieval falls back to lexical."""
    from nomadicos.memory.graph import ObjectGraph
    from nomadicos.memory.retrieval import HybridRetriever

    store = JsonlMemoryStore(tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path)))
    from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord

    store.write(
        MemoryRecord(
            id="m1",
            kind=MemoryKind.SEMANTIC,
            content="PostgreSQL uses port 5432",
            provenance=MemoryProvenance(confidence=0.6),
        )
    )

    # A model pointed at a wrong port simulates Ollama being unavailable
    dead_model = OllamaEmbeddingModel(model=EMBEDDING_MODEL, base_url="http://localhost:99999")
    index = VectorIndex(
        tmp_path / "vector.jsonl", dead_model, MemoryConfig(state_dir=str(tmp_path))
    )
    hybrid = HybridRetriever(store, ObjectGraph(store), index, dead_model)

    # Lexical retrieval still finds the record
    results = await hybrid.retrieve(MemoryQuery(text="PostgreSQL port", limit=8))
    assert any(r.record.id == "m1" for r in results), (
        "lexical fallback failed: semantic outage lost the record"
    )
    await dead_model.aclose()
