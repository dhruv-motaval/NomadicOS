"""Phase 14 tests: benchmark matrix + retrieval metrics (BP §212-216, §304-305, §314)."""

import pytest

from nomadicos.benchmark.benchmarks import BenchmarkMatrix
from nomadicos.benchmark.metrics import mrr, precision_at_k, recall_at_k


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b", "c"], ["a", "b"], 3) == 1.0
    assert recall_at_k(["a", "x", "y"], ["a", "b"], 3) == 0.5
    assert recall_at_k(["x"], ["a"], 1) == 0.0
    assert recall_at_k(["x"], [], 1) == 0.0


def test_precision_at_k() -> None:
    assert precision_at_k(["a", "b", "x"], ["a", "b"], 3) == pytest.approx(2 / 3)
    assert precision_at_k([], ["a"], 3) == 0.0


def test_mrr() -> None:
    assert mrr(["x", "a", "b"], ["a"]) == 0.5
    assert mrr(["a", "x"], ["a"]) == 1.0
    assert mrr(["x", "y"], ["a"]) == 0.0


def _corpus(n: int) -> list[tuple[str, list[float], dict]]:
    corpus = []
    for i in range(n):
        vector = [1.0 if j == i % 8 else 0.05 for j in range(8)]
        corpus.append((f"doc-{i}", vector, {"family": f"f{i % 8}"}))
    return corpus


def test_benchmark_matrix_perfect_recall_on_exact_engine() -> None:
    matrix = BenchmarkMatrix()
    corpus = _corpus(40)
    queries = [
        (corpus[0][1], ["doc-0"]),
        (corpus[8][1], ["doc-8", "doc-0"]),
        (corpus[16][1], ["doc-16"]),
    ]
    result = matrix.run(
        engine_name="native-exact", metric="cosine", corpus=corpus, queries=queries, top_k=5
    )
    assert result.recall_at_k == 1.0
    assert result.avg_latency_ms >= 0.0
    assert len(matrix.matrix()) == 1


def test_benchmark_matrix_rows_accumulate() -> None:
    matrix = BenchmarkMatrix()
    corpus = _corpus(16)
    queries = [(corpus[0][1], ["doc-0"])]
    matrix.run(
        engine_name="native-cosine", metric="cosine", corpus=corpus, queries=queries, top_k=3
    )
    matrix.run(engine_name="native-l2", metric="l2", corpus=corpus, queries=queries, top_k=3)
    rows = matrix.matrix()
    assert [r["engine"] for r in rows] == ["native-cosine", "native-l2"]


def test_benchmark_scores_are_deterministic() -> None:
    """BP §169: benchmark results are reproducible (quality metrics; wall-clock
    latency is hardware-dependent and excluded from the comparison)."""
    matrix = BenchmarkMatrix()
    corpus = _corpus(24)
    queries = [(corpus[2][1], ["doc-2"])]
    r1 = matrix.run(engine_name="native", metric="cosine", corpus=corpus, queries=queries, top_k=3)
    r2 = matrix.run(engine_name="native", metric="cosine", corpus=corpus, queries=queries, top_k=3)
    assert (r1.recall_at_k, r1.precision_at_k, r1.mrr) == (
        r2.recall_at_k,
        r2.precision_at_k,
        r2.mrr,
    )
