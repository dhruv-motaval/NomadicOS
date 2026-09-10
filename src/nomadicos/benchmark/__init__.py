"""Advanced optimization: benchmark matrix + retrieval metrics (BP §212-216, §304-305, §314).

V0 scope: measure, don't assume (BP §213). The matrix compares engine variants
(exact @ different metrics) with Recall@K against brute-force ground truth.
HNSW lands here only once these numbers exist (BP §305).
"""

from nomadicos.benchmark.benchmarks import BenchmarkMatrix
from nomadicos.benchmark.metrics import (
    mrr,
    precision_at_k,
    recall_at_k,
)

__all__ = ["BenchmarkMatrix", "mrr", "precision_at_k", "recall_at_k"]
