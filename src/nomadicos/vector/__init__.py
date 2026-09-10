"""Native Vector Engine V0 (BP §20-22, §303-312, §345-347; ADR-0003/0007/0008).

Exact search is the ground truth (BP §304). HNSW comes only after Recall@K
benchmarks against this baseline (BP §305).
"""

from nomadicos.vector.embeddings import Embedder, FakeEmbedder
from nomadicos.vector.engine import VectorEngine

__all__ = ["Embedder", "FakeEmbedder", "VectorEngine"]
