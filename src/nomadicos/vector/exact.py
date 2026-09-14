"""Exact-search vector store — the V0 ground truth (BP §21, §304, ADR-0007).

Correctness first: brute-force distance over NumPy-less pure Python keeps the
implementation auditable; swap-in optimized arrays is a Phase 14 concern.
Cosine similarity on L2-normalized vectors; ties resolved by insertion order.
"""

import json
import math
from pathlib import Path
from typing import Any

from nomadicos.vector.base import FORMAT_VERSION, VectorMatch, VectorStore

SNAPSHOT_NAME = "index.json"


class ExactVectorStore(VectorStore):
    def __init__(
        self,
        *,
        dimensions: int,
        metric: str = "cosine",
        embedding_model: str = "unset",
        embedding_version: str = "0",
    ) -> None:
        if metric not in ("cosine", "dot", "l2"):
            raise ValueError(f"unsupported metric {metric!r}")
        self.dimensions = dimensions
        self.metric = metric
        self.embedding_model = embedding_model
        self.embedding_version = embedding_version
        self._vectors: dict[str, list[float]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []  # insertion order for deterministic ties

    # ------------------------------------------------------------ validation

    def _check(self, vector_id: str, vector: list[float]) -> None:
        if not vector_id:
            raise ValueError("vector id must be non-empty")
        if len(vector) != self.dimensions:
            raise ValueError(
                f"vector dimension mismatch: expected {self.dimensions}, got {len(vector)}"
            )
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in vector):
            raise ValueError("vector components must be numbers")
        if any(math.isnan(x) or math.isinf(x) for x in vector):
            raise ValueError("vector components must be finite")

    # ------------------------------------------------------------------- BP §312

    def upsert(
        self, vector_id: str, vector: list[float], metadata: dict[str, Any] | None = None
    ) -> None:
        self._check(vector_id, vector)
        if vector_id not in self._vectors:
            self._order.append(vector_id)
        self._vectors[vector_id] = [float(x) for x in vector]
        self._metadata[vector_id] = dict(metadata or {})

    def get(self, vector_id: str) -> tuple[list[float], dict[str, Any]] | None:
        if vector_id not in self._vectors:
            return None
        return self._vectors[vector_id], self._metadata[vector_id]

    def delete(self, vector_id: str) -> bool:
        if vector_id in self._vectors:
            del self._vectors[vector_id]
            del self._metadata[vector_id]
            self._order.remove(vector_id)
            return True
        return False

    def search(
        self, vector: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[VectorMatch]:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        self._check("query", vector)
        candidates = [
            vid
            for vid in self._order
            if not filters or all(self._metadata[vid].get(k) == v for k, v in filters.items())
        ]
        scored = [(self._distance(vector, self._vectors[vid]), vid) for vid in candidates]
        if self.metric == "l2":
            scored.sort(key=lambda pair: (pair[0], candidates.index(pair[1])))
        else:
            scored.sort(key=lambda pair: (-pair[0], candidates.index(pair[1])))
        return [
            VectorMatch(
                vector_id=vid,
                score=round(score, 6),
                metadata=dict(self._metadata[vid]),
            )
            for score, vid in scored[:top_k]
        ]

    def count(self) -> int:
        return len(self._vectors)

    def stats(self) -> dict[str, Any]:
        return {
            "count": len(self._vectors),
            "dimensions": self.dimensions,
            "metric": self.metric,
            "embedding_model": self.embedding_model,
            "embedding_model_version": self.embedding_version,
            "format_version": FORMAT_VERSION,
            "engine": "exact",
        }

    # ------------------------------------------------------------- persistence

    def header(self) -> dict[str, Any]:
        from nomadicos.vector.base import format_version_header

        return format_version_header(
            dimensions=self.dimensions,
            metric=self.metric,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
        )

    def snapshot(self, directory: str | Path) -> Path:
        """Persist index + metadata with a versioned header (BP §307, ADR-0008)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "header": self.header(),
            "order": list(self._order),
            "vectors": self._vectors,
            "metadata": self._metadata,
        }
        path = directory / SNAPSHOT_NAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @classmethod
    def restore(cls, directory: str | Path) -> "ExactVectorStore":
        path = Path(directory) / SNAPSHOT_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        header = payload["header"]
        if header.get("format_version") != FORMAT_VERSION:
            raise ValueError(
                f"index format version mismatch: file={header.get('format_version')} "
                f"engine={FORMAT_VERSION}"
            )
        store = cls(
            dimensions=header["dimensions"],
            metric=header["metric"],
            embedding_model=header["embedding_model"],
            embedding_version=header["embedding_model_version"],
        )
        store._vectors = {k: list(v) for k, v in payload["vectors"].items()}
        store._metadata = payload["metadata"]
        store._order = list(payload["order"])
        return store

    # ---------------------------------------------------------------- internals

    def _distance(self, a: list[float], b: list[float]) -> float:
        if self.metric == "l2":
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))
        if self.metric == "dot":
            return sum(x * y for x, y in zip(a, b, strict=True))
        # cosine
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


__all__ = ["ExactVectorStore", "SNAPSHOT_NAME"]
