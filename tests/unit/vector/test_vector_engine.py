"""Phase 9 tests: VectorEngine + embeddings (BP §21, §303-312, §345-347)."""

import pytest

from nomadicos.vector.embeddings import FakeEmbedder
from nomadicos.vector.engine import VectorEngine


@pytest.fixture()
def engine(tmp_path) -> VectorEngine:
    return VectorEngine(tmp_path / "vector", FakeEmbedder(dimensions=384))


async def test_upsert_and_semantic_search(engine: VectorEngine) -> None:
    await engine.upsert_memory("memories", "v1", "PostgreSQL 18 migration fixed the bug")
    await engine.upsert_memory("memories", "v2", "Docker networking was misconfigured")
    await engine.upsert_memory("memories", "v3", "PostgreSQL backup policy decided")

    results = await engine.search("memories", "PostgreSQL")
    assert len(results) >= 2
    # both PostgreSQL-related documents rank above the Docker one
    top_texts = [m.metadata["text"] for m in results[:2]]
    assert all("PostgreSQL" in t for t in top_texts)


async def test_metadata_filtering(engine: VectorEngine) -> None:
    await engine.upsert_memory(
        "memories", "v1", "PostgreSQL 18 migration", {"project_id": "project-x"}
    )
    await engine.upsert_memory(
        "memories", "v2", "PostgreSQL backup policy", {"project_id": "project-y"}
    )
    results = await engine.search(
        "memories", "PostgreSQL", top_k=5, filters={"project_id": "project-x"}
    )
    assert len(results) == 1
    assert results[0].metadata["project_id"] == "project-x"


def test_index_persists_and_restores(engine: VectorEngine, tmp_path) -> None:
    asyncio_run = __import__("asyncio").run

    async def seed() -> None:
        await engine.upsert_memory("memories", "v1", "PostgreSQL 18 migration")

    asyncio_run(seed())
    engine.persist("memories")

    fresh_engine = VectorEngine(engine._base, FakeEmbedder(dimensions=384))
    store = fresh_engine.get_or_create_index("memories")
    assert store.count() == 1
    stats = store.stats()
    assert stats["embedding_model"] == "fake/hash-embed"
    assert stats["dimensions"] == 384


async def test_embedding_model_mismatch_rejected(engine: VectorEngine, tmp_path) -> None:
    """BP §107: never silently mix incompatible embedding models."""
    await engine.upsert_memory("memories", "v1", "some memory")
    engine.persist("memories")

    different = VectorEngine(
        tmp_path / "vector", FakeEmbedder(dimensions=384, model_id="other-model")
    )
    with pytest.raises(ValueError, match="embedding model"):
        different.get_or_create_index("memories")


def test_embedder_is_deterministic_and_normalized() -> None:
    import asyncio

    embedder = FakeEmbedder(dimensions=384)
    v1 = asyncio.run(embedder.embed("the quick brown fox"))
    v2 = asyncio.run(embedder.embed("the quick brown fox"))
    assert v1 == v2
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6  # L2-normalized (cosine-ready)


def test_embedder_info_carries_versioning() -> None:
    info = FakeEmbedder(dimensions=384).info
    assert info.dimensions == 384
    assert info.version == "1.0.0"
    assert info.model_id
