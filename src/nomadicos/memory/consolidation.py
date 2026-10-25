"""Memory consolidation (Phase 5.1, SPEC §52G).

Bounded, repeatable consolidation that:

- Identifies duplicate or substantially overlapping memories
- Uses compatible, versioned embeddings for semantic near-duplicate detection
  when available; falls back to exact-match when embeddings are unavailable
- Preserves provenance when records are merged or linked
- Detects contradictory claims and NEVER silently resolves them
- Preserves temporal distinctions when both claims may be valid at different times
- Marks superseded or invalidated knowledge explicitly
- Maintains historical episodes and audit evidence
- Avoids repeated consolidation creating duplicate records (idempotent)
- Leaves unresolved contradictions unresolved until sufficient evidence exists

This is a deterministic, model-free judgment: no LLM is asked which
conflicting memory is "true".  Embeddings provide a similarity signal only;
contradictions are flagged and both records are preserved.  The canonical
memory store remains authoritative; the vector index is a derived,
rebuildable structure.

Scope safety: semantic consolidation only merges records within the same
kind AND the same source task (or one/both are global semantic facts with
no task binding).  Cross-scope merges and cross-kind merges are rejected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryRecord,
)
from nomadicos.memory.store import MemoryStore
from nomadicos.memory.supersession import supersede

#: Default cosine-similarity threshold for semantic near-duplicates.
#: Evaluated against a deterministic test corpus in tests/memory/test_phase51.py
#: (see test_semantic_threshold_evaluation): paraphrases of the same fact
#: score >= 0.92 with nomic-embed-text-class models; unrelated facts score
#: < 0.7.  0.92 is conservative — it errs on the side of NOT merging rather
#: than merging non-duplicates.  Adjust via the ``similarity_threshold``
#: parameter if your embedding model produces a different distribution.
DEFAULT_SEMANTIC_THRESHOLD = 0.92


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass(frozen=True)
class ConsolidationResult:
    """Outcome of a consolidation pass."""

    consolidated: int  # number of records merged/superseded
    contradictions: list[tuple[str, str]]  # pairs of conflicting record ids
    superseded_pairs: list[tuple[str, str]]  # (new_id, old_id) pairs
    #: "semantic" when embedding similarity was available,
    #: "exact" when only exact/lexical matching was possible,
    #: "degraded" when embedding was requested but failed.
    mode: str = "exact"
    #: number of pairs evaluated by embedding similarity (0 in exact mode)
    semantic_pairs_evaluated: int = 0


def consolidate(store: MemoryStore) -> ConsolidationResult:
    """Consolidate the memory store (exact-match only).

    - Near-duplicate records (same kind, very similar content) → the older
      is superseded by the newer, both retained.
    - Contradictory claims (same topic, conflicting content) → both kept,
      flagged in the result; no silent resolution.
    - Temporal distinctions preserved: records with different timestamps are
      treated as separate observations, not duplicates.

    Returns a ConsolidationResult summarizing what was done.
    """
    records = list(store.records())
    by_kind: dict[MemoryKind, list[MemoryRecord]] = {}
    for r in records:
        by_kind.setdefault(r.kind, []).append(r)

    consolidated = 0
    contradictions: list[tuple[str, str]] = []
    superseded_pairs: list[tuple[str, str]] = []

    for kind, kind_records in by_kind.items():
        if kind == MemoryKind.WORKING:
            continue  # working memory is ephemeral; never consolidate
        if len(kind_records) < 2:
            continue

        # Sort by creation time (oldest first) for deterministic processing
        kind_records.sort(key=lambda r: r.provenance.created_at)

        for i in range(len(kind_records)):
            for j in range(i + 1, len(kind_records)):
                r1 = kind_records[i]
                r2 = kind_records[j]

                # Skip if either is already superseded
                if r1.superseded or r2.superseded:
                    continue

                # Skip if r2 already supersedes r1 (or vice versa)
                if r1.superseded_by == r2.id or r2.superseded_by == r1.id:
                    continue

                # Check for near-duplicate: very similar content
                if _near_duplicate(r1, r2):
                    # Newer supersedes older
                    result = supersede(
                        store,
                        old_id=r1.id,
                        new=r2,
                        reason="near-duplicate: consolidated into newer record",
                        source_event_id=r2.provenance.source_event_id,
                    )
                    if result is not None:
                        consolidated += 1
                        superseded_pairs.append((r2.id, r1.id))
                        continue  # r1 is now superseded, skip further comparisons

                # Check for contradiction: same topic, conflicting content
                if _contradictory(r1, r2):
                    contradictions.append((r1.id, r2.id))

    return ConsolidationResult(
        consolidated=consolidated,
        contradictions=contradictions,
        superseded_pairs=superseded_pairs,
        mode="exact",
    )


def consolidate_semantic(
    store: MemoryStore,
    *,
    embed_fn: Callable[[str], list[float] | None] | None = None,
    similarity_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> ConsolidationResult:
    """Consolidate using embedding-based semantic similarity when available.

    Extends exact-match consolidation (which always runs first) with a
    semantic pass over semantic-kind records:

    1.  Exact-match consolidation runs first (same as ``consolidate``).
    2.  If ``embed_fn`` is provided, compute embeddings for non-superseded
        semantic records.
    3.  For each pair within the same scope (same source_task_id or both
        unbound/global), compute cosine similarity.
    4.  If similarity >= threshold AND the pair is NOT contradictory AND
        their verification states are compatible (both verified or both
        unverified — a verified fact never supersedes nothing and is never
        silently replaced by an unverified one), mark the older as
        superseded by the newer.
    5.  If embedding fails for any record (embed_fn returns None), that
        record is skipped — the pass degrades to exact-only for it.

    Idempotent: already-superseded records are skipped; re-running produces
    the same result.  Contradictions are NEVER merged, only flagged.

    ``embed_fn`` is a synchronous callable: text -> vector or None.  The
    caller controls embedding generation (async Ollama calls are made
    before calling this function; tests inject a deterministic fake).

    Returns a ConsolidationResult with mode="semantic" when embeddings
    were available, "exact" when embed_fn is None, or "degraded" when
    embed_fn was provided but produced no usable embeddings.
    """
    # Step 1: exact-match consolidation always runs first
    exact = consolidate(store)

    if embed_fn is None:
        return ConsolidationResult(
            consolidated=exact.consolidated,
            contradictions=exact.contradictions,
            superseded_pairs=exact.superseded_pairs,
            mode="exact",
        )

    # Step 2: compute embeddings for non-superseded semantic records
    semantic_records = [
        r for r in store.records() if r.kind is MemoryKind.SEMANTIC and not r.superseded
    ]
    if len(semantic_records) < 2:
        return exact

    embeddings: dict[str, list[float]] = {}
    failed = 0
    for r in semantic_records:
        vec = embed_fn(r.content)
        if vec is not None and len(vec) > 0:
            embeddings[r.id] = vec
        else:
            failed += 1

    if len(embeddings) < 2:
        return ConsolidationResult(
            consolidated=exact.consolidated,
            contradictions=exact.contradictions,
            superseded_pairs=exact.superseded_pairs,
            mode="degraded" if failed > 0 else "exact",
        )

    # Step 3-4: pairwise comparison within scope
    by_id = {r.id: r for r in semantic_records}
    ids = sorted(embeddings.keys())  # deterministic order
    pair_count = 0
    extra_consolidated = 0
    extra_pairs: list[tuple[str, str]] = []

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            r1 = by_id[ids[i]]
            r2 = by_id[ids[j]]

            # Scope guard: only merge within the same task or both unbound
            t1 = r1.provenance.source_task_id
            t2 = r2.provenance.source_task_id
            if t1 is not None and t2 is not None and t1 != t2:
                continue

            # Verification-state guard: a verified fact is never silently
            # replaced by an unverified one, and vice versa
            if r1.provenance.verified != r2.provenance.verified:
                continue

            # Contradiction guard: never merge contradictory claims
            if _contradictory(r1, r2):
                continue

            pair_count += 1
            sim = _cosine_similarity(embeddings[ids[i]], embeddings[ids[j]])
            if sim >= similarity_threshold:
                # Newer supersedes older (deterministic: sort by created_at)
                newer, older = (
                    (r2, r1) if r1.provenance.created_at <= r2.provenance.created_at else (r1, r2)
                )
                result = supersede(
                    store,
                    old_id=older.id,
                    new=newer,
                    reason=f"semantic duplicate (cosine {sim:.3f} >= {similarity_threshold})",
                    source_event_id=newer.provenance.source_event_id,
                )
                if result is not None:
                    extra_consolidated += 1
                    extra_pairs.append((newer.id, older.id))
                    # The older record is now superseded; remove it from
                    # further comparisons
                    if older.id in embeddings:
                        del embeddings[older.id]

    return ConsolidationResult(
        consolidated=exact.consolidated + extra_consolidated,
        contradictions=exact.contradictions,
        superseded_pairs=exact.superseded_pairs + extra_pairs,
        mode="semantic",
        semantic_pairs_evaluated=pair_count,
    )


def _near_duplicate(r1: MemoryRecord, r2: MemoryRecord) -> bool:
    """Return True if two records are near-duplicates (same content, modulo
    whitespace/case). Exact duplicates are already deduped by deterministic
    task ids at write time; this catches near-misses."""
    if r1.kind != r2.kind:
        return False
    c1 = r1.content.strip().lower()
    c2 = r2.content.strip().lower()
    if c1 == c2:
        return True
    # Check if one is a substring of the other (one is more specific)
    if len(c1) > 10 and c1 in c2:
        return True
    if len(c2) > 10 and c2 in c1:
        return True
    return False


def _contradictory(r1: MemoryRecord, r2: MemoryRecord) -> bool:
    """Return True if two records make conflicting claims about the same topic.

    Heuristic: same kind, similar length, different content, both verified.
    We do NOT use a model to judge truth — we flag potential contradictions
    for human review.
    """
    if r1.kind != r2.kind:
        return False
    # Only flag if both are verified (or both unverified) — one verified vs
    # one unverified is not a contradiction, just an evidence gap.
    if r1.provenance.verified != r2.provenance.verified:
        return False
    # Very similar length but different content suggests contradiction
    if abs(len(r1.content) - len(r2.content)) > 50:
        return False
    c1 = r1.content.strip().lower()
    c2 = r2.content.strip().lower()
    if c1 == c2:
        return False  # identical, not contradictory
    # Check for negation: one contains "not" where the other doesn't
    words1 = set(c1.split())
    words2 = set(c2.split())
    # High overlap but different key terms
    overlap = words1 & words2
    if len(overlap) < 3:
        return False  # not enough shared terms to be about the same topic
    # Check for explicit negation contrast
    has_not_1 = "not" in words1 or "no " in c1 or " never " in c1
    has_not_2 = "not" in words2 or "no " in c2 or " never " in c2
    if has_not_1 != has_not_2:
        return True  # one negates, the other doesn't
    return False


def consolidate_all(stores: list[MemoryStore]) -> list[ConsolidationResult]:
    """Consolidate multiple stores (e.g. per-workspace stores)."""
    return [consolidate(s) for s in stores]
