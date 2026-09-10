# ADR-0017: Scheduler — In-Process Priority Scheduler

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §244-249 (Q17)
- **Source:** Owner answers 2026-09-04

## Decision

Use an in-process priority scheduler for v0.1. Priority order:

```text
FOREGROUND USER TASK
>
BACKGROUND LEARNING
>
BENCHMARK
>
BACKUP
>
MAINTENANCE
```

## Consequences

- Avoids OS-specific scheduling complexity initially.
- Implemented as asyncio tasks with explicit budgets (BP §247 job classes each carry
  a resource policy).
- User tasks always preempt learning under resource pressure (BP §244-245, §246).
