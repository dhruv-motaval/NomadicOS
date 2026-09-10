"""Retrieval quality metrics (BP §108): Recall@K, Precision@K, MRR."""

from collections.abc import Sequence


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of relevant documents found in the top-k."""
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return len(top & set(relevant)) / len(set(relevant))


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Fraction of the top-k that is relevant."""
    if k <= 0:
        return 0.0
    top = retrieved[:k]
    if not top:
        return 0.0
    return len(set(top) & set(relevant)) / len(set(top))


def mrr(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    """Mean reciprocal rank of the first relevant hit."""
    relevant_set = set(relevant)
    for index, doc in enumerate(retrieved, start=1):
        if doc in relevant_set:
            return 1.0 / index
    return 0.0


__all__ = ["mrr", "precision_at_k", "recall_at_k"]
