# ADR-0007: Native Vector Engine V0 — Exact Search First

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §20-22, §303-305, §313 (Q7)
- **Source:** Owner answers 2026-09-04

## Decision

Build our own exact-search baseline first. **Do NOT make pgvector/Qdrant/LanceDB the
production memory backend** — external vector systems are experimental comparison
backends only.

```text
vectors → exact search → ground truth → ANN implementation → Recall@K comparison
```

## Consequences

- No HNSW before exact-search benchmarks exist (BP §305).
- The benchmark matrix (BP §314: Native vs Qdrant vs LanceDB) is the acceptance gate.
- Scope-locked to BP §21's design areas + §312 API; correctness → persistence → ANN
  → performance (BP §303).
