# ADR-0018: Windows Sandbox — Layered Restrictions, Honest About Limits

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §35, §91 (Q18)
- **Source:** Owner answers 2026-09-04

## Decision

For v0.1, use layered Windows restrictions rather than pretending we have perfect
sandbox isolation. Use where appropriate: restricted process mechanisms, Windows Job
Objects, task-specific workspaces (BP §92), resource limits, network policy.
Full VM/container-grade isolation is future work.

## Consequences

- The **Sandbox abstraction must be stronger than its first implementation**:

```text
Sandbox
  |
  +-- filesystem policy
  +-- process limits
  +-- network policy
  +-- working directory
  +-- resource limits
```

- Security Gate risk decisions treat SANDBOXED as "reduced risk", never "contained" —
  fail-closed still applies to unknown/high-risk operations (BP §85).
- VM/container isolation is the documented upgrade path, not a v0.1 promise.
