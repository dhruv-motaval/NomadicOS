# ADR-0003: Embedding Model — Configurable 384-d Baseline, Locked After Benchmark

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §345-347, §107, §311 (Q3)
- **Source:** Owner answers 2026-09-04

## Decision

Do not permanently lock the embedding model before benchmarking. Initial baseline:
a relatively small local embedding model around **384 dimensions**.

Make the embedding model configurable and versioned. Every vector index must record:
embedding model, version, dimensions, distance metric (BP §107, §311).

## Consequences

- Lower storage, faster development, easier native vector-engine testing.
- The final canonical embedding model is selected after the local retrieval
  benchmark (BP §214) — until then the baseline is provisional.
- Changing embedding models later follows BP §107: version old → build new index →
  benchmark retrieval → migrate. Never mix incompatible dimensions/models.
