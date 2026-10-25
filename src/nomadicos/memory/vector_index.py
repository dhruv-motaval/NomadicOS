"""Vector index for semantic memory retrieval (Phase 5, SPEC §52G).

Persists embedding vectors alongside canonical memory records in the existing
JSONL memory store. Each vector record carries the embedding model id and
dimension so that index rebuilds can be triggered when the model changes or
dimensions are incompatible.

Design:

- **Additive only**: vectors are stored as separate ``embedding`` storage_type
  records keyed by the memory record id they embed. The canonical memory
  record (in ``memory.jsonl``) is never modified by the vector index.
- **Fail-safe**: any vector index problem (corruption, dimension mismatch,
  missing model) returns empty results or triggers a silent rebuild; it never
  blocks memory reads/writes or task execution.
- **Idempotent upserts**: re-embedding the same text+model produces the same
  vector id, so re-indexing is a no-op.
- **Stale detection**: if the stored model_id/dimension differs from the
  current embedding model, the index is considered stale and all vectors are
  discarded (rebuild on next add).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.embedding import EmbeddingError, EmbeddingModel

_EMBEDDING = "embedding"
_VECTOR_META = "vector_meta"


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 3) -> None:
    import time

    for _ in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(0.05 * (2**_))
    os.replace(tmp, target)


import os  # noqa: E402  (placed after first use for readability of _replace_with_retry)


class VectorIndex:
    """Persistent vector index for semantic search over memory records.

    Vectors are stored as ``embedding`` storage_type records in the same
    JSONL file as memory records, keyed by the memory record id. A separate
    ``vector_meta`` record stores the model id, dimension, and total count
    so that stale or incompatible indexes can be detected and rebuilt.

    All operations are fail-safe: exceptions are caught and the index
    degrades gracefully (empty results or silent rebuild).
    """

    def __init__(
        self,
        store_path: Path | str,
        model: EmbeddingModel,
        config: MemoryConfig | None = None,
    ) -> None:
        self.store_path = Path(store_path)
        self.model = model
        self.config = config or MemoryConfig()
        self._vectors: dict[str, list[float]] = {}
        self._meta: dict[str, Any] = {}
        self._loaded = False
        self._dirty = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load()
        self._loaded = True

    def _load(self) -> None:
        """Load all embedding vectors and metadata from the store file."""
        self._vectors = {}
        self._meta = {}
        if not self.store_path.exists():
            return
        try:
            text = self.store_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return  # fail-safe: empty index
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                tag = raw.get("storage_type")
                data = raw.get("data", {})
            except (json.JSONDecodeError, KeyError, TypeError):
                continue  # skip corrupt lines
            if tag == _EMBEDDING:
                rid = data.get("memory_record_id")
                vec = data.get("vector")
                if rid and isinstance(vec, list) and all(isinstance(v, (int, float)) for v in vec):
                    self._vectors[str(rid)] = [float(v) for v in vec]
            elif tag == _VECTOR_META:
                self._meta = dict(data)

    def _stale(self) -> bool:
        """Return True if the index metadata doesn't match the current model."""
        if not self._meta:
            return True  # no meta = stale (empty index)
        if self._meta.get("model_id") != self.model.model_id:
            return True
        if self._meta.get("embedding_dim") != self.model.embedding_dim:
            return True
        return False

    def _persist(self) -> None:
        """Atomically rewrite the store file with vectors + metadata."""
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.store_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # Write meta first
                fh.write(
                    json.dumps(
                        {
                            "storage_type": _VECTOR_META,
                            "data": {
                                "model_id": self.model.model_id,
                                "embedding_dim": self.model.embedding_dim,
                                "vector_count": len(self._vectors),
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                for rid, vec in self._vectors.items():
                    fh.write(
                        json.dumps(
                            {
                                "storage_type": _EMBEDDING,
                                "data": {
                                    "memory_record_id": rid,
                                    "vector": vec,
                                    "model_id": self.model.model_id,
                                    "embedding_dim": self.model.embedding_dim,
                                },
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            _replace_with_retry(Path(tmp), self.store_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def add(self, memory_record_id: str, text: str) -> None:
        """Embed ``text`` and store the vector keyed by ``memory_record_id``.

        Fail-safe: any embedding or storage failure is silently ignored —
        the vector simply won't be available for semantic search.
        """
        self._ensure_loaded()
        if self._stale():
            self._rebuild()
        try:
            vector = await self.model.embed_one(text)
        except EmbeddingError:
            return  # fail-safe: no vector for this text
        self._vectors[memory_record_id] = vector
        self._dirty = True
        self._maybe_persist()

    def _maybe_persist(self) -> None:
        if self._dirty:
            self._persist()
            self._dirty = False

    def _rebuild(self) -> None:
        """Drop all vectors (stale index) — they will be re-embedded on next add."""
        self._vectors = {}
        self._meta = {}
        self._dirty = True
        self._maybe_persist()

    def search(self, query_vector: list[float], top_k: int = 8) -> list[tuple[str, float]]:
        """Cosine-similarity search over stored vectors.

        Returns list of (memory_record_id, cosine_similarity) sorted by
        descending similarity. Returns empty list if the index is empty,
        stale, or any error occurs.
        """
        self._ensure_loaded()
        if self._stale() or not self._vectors:
            return []
        if len(query_vector) != self.model.embedding_dim:
            return []  # dimension mismatch → fail-safe

        scores: list[tuple[str, float]] = []
        q_norm = sum(v * v for v in query_vector) ** 0.5
        if q_norm == 0:
            return []
        for rid, vec in self._vectors.items():
            v_norm = sum(v * v for v in vec) ** 0.5
            if v_norm == 0:
                continue
            sim = sum(a * b for a, b in zip(query_vector, vec, strict=True)) / (q_norm * v_norm)
            scores.append((rid, sim))
        scores.sort(key=lambda x: (-x[1], x[0]))
        return scores[:top_k]

    def has_vector(self, memory_record_id: str) -> bool:
        self._ensure_loaded()
        if self._stale():
            return False
        return memory_record_id in self._vectors

    def remove(self, memory_record_id: str) -> None:
        """Remove a vector (e.g. when the memory record is superseded/deleted)."""
        self._ensure_loaded()
        if memory_record_id in self._vectors:
            del self._vectors[memory_record_id]
            self._dirty = True
            self._maybe_persist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0.0 for zero-length vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
