"""VectorStore contract (BP §21, §312) + index format metadata (BP §311, ADR-0008).

The on-disk format is provisional until V0 experimentation completes (ADR-0008),
but the *metadata contract* (format_version, dimensions, metric, embedding model)
is fixed now so nothing silently mixes incompatible indexes (BP §107).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

FORMAT_VERSION = "0.1"
VALID_METRICS = frozenset({"cosine", "l2", "dot"})
VALID_DIMENSIONS = (384, 768)  # ADR-0003 baseline shapes


@dataclass(frozen=True, slots=True)
class VectorMatch:
    vector_id: str
    score: float
    metadata: dict[str, Any]


def validate_metric(metric: str) -> str:
    if metric not in VALID_METRICS:
        raise ValueError(f"unsupported metric {metric!r}; expected one of {sorted(VALID_METRICS)}")
    return metric


def format_version_header(
    *, dimensions: int, metric: str, embedding_model: str, embedding_version: str
) -> dict[str, Any]:
    """Index header persisted with every snapshot (BP §311, §107)."""
    return {
        "format_version": FORMAT_VERSION,
        "dimensions": dimensions,
        "metric": validate_metric(metric),
        "embedding_model": embedding_model,
        "embedding_model_version": embedding_version,
        "index_parameters": {},
    }


class VectorStore(ABC):
    """BP §312 conceptual API. IDs and metadata are caller-managed."""

    @abstractmethod
    def upsert(
        self, vector_id: str, vector: list[float], metadata: dict[str, Any] | None = None
    ) -> None: ...

    @abstractmethod
    def get(self, vector_id: str) -> tuple[list[float], dict[str, Any]] | None: ...

    @abstractmethod
    def delete(self, vector_id: str) -> bool: ...

    @abstractmethod
    def search(
        self, vector: list[float], top_k: int = 5, filters: dict[str, Any] | None = None
    ) -> list[VectorMatch]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def stats(self) -> dict[str, Any]: ...
