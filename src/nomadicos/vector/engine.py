"""VectorEngine: index management over ExactVectorStore (BP §21, §312, ADR-0008).

One engine owns multiple named indexes. Every index is a directory under
`NOMADICOS_DATA_DIR/data/vector/<index-id>/` with a versioned header
(ADR-0008). Indexes are embedding-model-bound (BP §107) — a query embeds with
the index's own embedder config, never mixing dimensions/models.
"""

import asyncio
from typing import Any

from nomadicos.core.logging import get_logger
from nomadicos.vector.base import VectorMatch
from nomadicos.vector.embeddings import Embedder, EmbedderInfo
from nomadicos.vector.exact import ExactVectorStore

logger = get_logger("vector.engine")


class VectorEngine:
    def __init__(self, base_dir: Any, embedder: Embedder) -> None:
        from pathlib import Path

        self._base = Path(base_dir)
        self._embedder = embedder
        self._indexes: dict[str, ExactVectorStore] = {}

    @property
    def embedder_info(self) -> EmbedderInfo:
        return self._embedder.info

    def get_or_create_index(self, index_id: str, *, metric: str = "cosine") -> ExactVectorStore:
        """Open an existing index (restored from disk) or create a new one."""
        if index_id in self._indexes:
            return self._indexes[index_id]
        directory = self._base / index_id
        snapshot = directory / "index.json"
        if snapshot.exists():
            store = ExactVectorStore.restore(directory)
            if store.embedding_model != self._embedder.info.model_id:
                raise ValueError(
                    f"index {index_id} was built with embedding model "
                    f"{store.embedding_model!r}, current embedder is "
                    f"{self._embedder.info.model_id!r} (BP §107: never mix models)"
                )
        else:
            store = ExactVectorStore(
                dimensions=self._embedder.info.dimensions,
                metric=metric,
                embedding_model=self._embedder.info.model_id,
                embedding_version=self._embedder.info.version,
            )
        self._indexes[index_id] = store
        return store

    async def upsert_memory(
        self,
        index_id: str,
        vector_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Embed text locally and store (BP §345: no remote embedding)."""
        store = self.get_or_create_index(index_id)
        vector = await self._embedder.embed(text)
        meta = dict(metadata or {})
        meta["text"] = text  # retrieval needs the source text (BP §206 provenance)
        store.upsert(vector_id, vector, meta)

    async def search(
        self,
        index_id: str,
        query: str,
        *,
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorMatch]:
        """Semantic search with metadata filtering (BP §306)."""
        store = self.get_or_create_index(index_id)
        vector = await self._embedder.embed(query)
        return store.search(vector, top_k=top_k, filters=filters)

    async def delete(self, index_id: str, vector_id: str) -> bool:
        store = self.get_or_create_index(index_id)
        return store.delete(vector_id)

    def persist(self, index_id: str) -> Any:
        """Snapshot one index (BP §307)."""
        store = self.get_or_create_index(index_id)
        path = store.snapshot(self._base / index_id)
        logger.info("vector index persisted index=%s", index_id)
        return path

    def persist_all(self) -> list[Any]:
        return [self.persist(index_id) for index_id in self._indexes]

    def stats(self, index_id: str) -> dict[str, Any]:
        return self.get_or_create_index(index_id).stats()

    async def close(self) -> None:
        await asyncio.to_thread(self.persist_all)


__all__ = ["VectorEngine"]
