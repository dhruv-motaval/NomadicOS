# ADR-0012: Process Model — Single asyncio Process, Conservative Subprocesses

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §91, §129, §247-249 (Q12)
- **Source:** Owner answers 2026-09-04

## Decision

Use a single main Python process with asyncio for v0.1. Use supervised subprocesses
only where the actual workload requires them.

Multi-process IPC creates unnecessary complexity too early — but long-running/high-risk
operations are isolated behind a **process execution abstraction** so subprocess
isolation can be added without redesigning the Agent Runtime.

```text
Agent Runtime
    |
    +-- in-process logic
    |
    +-- controlled worker/subprocess when required
```

## Consequences

- Design process boundaries now; implement them conservatively.
- No IPC infrastructure initially (BP §129 applies when workers exist).
- Terminal, computer-control, and long jobs are the first candidates for the
  process execution abstraction.
