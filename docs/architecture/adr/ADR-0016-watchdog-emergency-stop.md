# ADR-0016: Watchdog & Emergency Stop — In-Process Watchdog, Out-of-Band Stop

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §70, §121-122 (Q16)
- **Source:** Owner answers 2026-09-04

## Decision

**Watchdog:** in-process supervisor for v0.1 (BP §70 responsibilities: detect hangs,
loops, excessive resource usage, repeated failures; cancel runaway tasks; record events;
never model-controlled).

**Emergency stop:** out-of-band control mechanism that does not depend on model
cooperation (BP §122). The agent must not be capable of intercepting its own emergency stop.

## Consequences

- Enforce: max task duration, max steps, max retries, resource budgets (BP §72, §52).
- A stop signal is accessible outside model reasoning — CLI/signal-level, wired
  directly into the task state machine (BP §137).
- Full watchdog process separation is future work (per ADR-0012 process model).
