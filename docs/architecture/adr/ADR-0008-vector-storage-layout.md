# ADR-0008: Vector Data Storage Layout

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §21, §103, §106, §311 (Q8)
- **Source:** Owner answers 2026-09-04

## Decision

Use a dedicated local vector-data directory under `NOMADICOS_DATA_DIR`:

```text
data/
  vector/
    <index-id>/
```

Store explicitly: `format_version`, `dimensions`, `metric`, embedding model/version,
index metadata. V0 uses simple local snapshot structures.

## Consequences

- The on-disk format is **not permanent** until V0 experimentation is complete.
- Storage lives behind the `VectorStore` interface (BP §209, §312); WAL (§308) and
  compaction (§309) come after basic storage correctness.
- The directory is excluded from sync-heavy locations and VCS (ADR-0009, environment config).
