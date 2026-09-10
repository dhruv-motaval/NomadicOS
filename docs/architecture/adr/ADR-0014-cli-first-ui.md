# ADR-0014: CLI-First UI, No Mandatory HTTP API in v0.1

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §54-55, §127-128 (Q14, F2)
- **Source:** Owner answers 2026-09-04

## Decision

**CLI-first for v0.1.** After the core runtime works, add a local web UI/dashboard.
An internal HTTP API is not required for the first runtime if direct in-process
interfaces are sufficient. If a local HTTP API is introduced, it must be
authenticated/authorized (BP §127-128).

## Consequences

- Frontend work never blocks the Agent Runtime.
- Logs + CLI are the observability surface during early development (BP §134).
- The future dashboard (BP §54-55, §233, §279-280) implements against the same
  in-process interfaces the CLI already uses.
