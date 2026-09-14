"""Benchmark matrix (BP §314): Native engine variants measured, not assumed.

Each run: seed a corpus, run known-relevant queries, measure Recall@K /
Precision@K / MRR / latency against brute-force ground truth. This is the
gate any future ANN implementation must pass (BP §305).
"""

import time
from dataclasses import dataclass, field
from typing import Any

from nomadicos.benchmark.metrics import mrr, precision_at_k, recall_at_k
from nomadicos.vector.exact import ExactVectorStore


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    engine_name: str
    metric: str
    corpus_size: int
    queries: int
    top_k: int
    recall_at_k: float
    precision_at_k: float
    mrr: float
    avg_latency_ms: float


@dataclass
class BenchmarkMatrix:
    """BP §314: rows per engine variant; extend as backends are added (§213)."""

    results: list[BenchmarkResult] = field(default_factory=list)

    def run(
        self,
        *,
        engine_name: str,
        metric: str,
        corpus: list[tuple[str, list[float], dict[str, Any]]],
        queries: list[tuple[list[float], list[str]]],  # (vector, relevant_ids)
        top_k: int = 5,
    ) -> BenchmarkResult:
        store = ExactVectorStore(dimensions=len(corpus[0][1]) if corpus else 1, metric=metric)
        for vector_id, vector, metadata in corpus:
            store.upsert(vector_id, vector, metadata)

        recalls, precisions, rrs, latencies = [], [], [], []
        for vector, relevant in queries:
            started = time.monotonic()
            matches = store.search(vector, top_k=top_k)
            latencies.append((time.monotonic() - started) * 1000)
            retrieved = [m.vector_id for m in matches]
            recalls.append(recall_at_k(retrieved, relevant, top_k))
            precisions.append(precision_at_k(retrieved, relevant, top_k))
            rrs.append(mrr(retrieved, relevant))

        result = BenchmarkResult(
            engine_name=engine_name,
            metric=metric,
            corpus_size=len(corpus),
            queries=len(queries),
            top_k=top_k,
            recall_at_k=round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
            precision_at_k=round(sum(precisions) / len(precisions), 4) if precisions else 0.0,
            mrr=round(sum(rrs) / len(rrs), 4) if rrs else 0.0,
            avg_latency_ms=round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        )
        self.results.append(result)
        return result

    def matrix(self) -> list[dict[str, Any]]:
        return [
            {
                "engine": r.engine_name,
                "metric": r.metric,
                "corpus": r.corpus_size,
                "top_k": r.top_k,
                "recall_at_k": r.recall_at_k,
                "precision_at_k": r.precision_at_k,
                "mrr": r.mrr,
                "avg_latency_ms": r.avg_latency_ms,
            }
            for r in self.results
        ]


__all__ = ["BenchmarkMatrix", "BenchmarkResult"]
