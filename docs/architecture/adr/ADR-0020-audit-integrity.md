# ADR-0020: Audit Integrity — Append-Only + Optional Hash-Chain Digest

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §41-42, §123, §251 (Q20)
- **Source:** Owner answers 2026-09-04

## Decision

v0.1 audit integrity:

- Append-oriented PostgreSQL audit table
- Local file mirror where useful
- Optional hash-chain digest

Full cryptographic tamper-evident infrastructure can come later (BP §251 lists hash
chaining as a potential future mechanism).

## Consequences

- Audit entries are **never controlled by the model** (BP §42, §82).
- Security events are higher priority than ordinary logs (BP §123).
- Audit initializes before autonomous execution (BP §234) and is flushed on shutdown
  (BP §235).
