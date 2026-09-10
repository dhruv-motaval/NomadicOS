# ADR-0011: Backup Strategy — pg_dump + Vector Snapshots

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §43, §105-106 (Q11)
- **Source:** Owner answers 2026-09-04

## Decision

Use **pg_dump-based logical backups** for v0.1. Vector data gets its own versioned
snapshot/backup mechanism. Future filesystem/storage snapshots can be added later.

## Consequences

- BackupManager must know how to: create backup, verify backup, restore, record
  backup version, and **test restore** (BP §43, §362).
- Critical backups are encrypted (BP §43).
- Before risky migrations: snapshot PostgreSQL + vector store, record version,
  migrate, verify (BP §105).
