# ADR-0027: v0.1 Development Phase Order (Authoritative)

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §77, §375 (Section K of owner answers)
- **Source:** Owner answers 2026-09-04

## Decision

The authoritative v0.1 implementation order:

```text
PHASE 0   Repository + tooling + interfaces
PHASE 1   PostgreSQL + core state
PHASE 2   Local model runtime + registry
PHASE 3   Security Gate + policies + permissions + audit
PHASE 4   Tool Gateway + filesystem + terminal
PHASE 5   Vision + Gemma adapter
PHASE 6   Computer control
PHASE 7   Browser + controlled Internet
PHASE 8   Memory Engine
PHASE 9   Native Vector Engine exact-search baseline
PHASE 10  Evaluation + verification
PHASE 11  Experience system
PHASE 12  Adaptive model selection
PHASE 13  Self-improvement + benchmarks
PHASE 14  Advanced vector engine / optimization
```

Do **not** skip the Security Gate to reach computer control faster (BP §375).

## Consequences

- This refines BP §77's phase list into the execution plan; BP §375's start order
  (`PostgreSQL → Core → Security → Local Model Runtime → ...`) is unchanged in spirit.
- Phase 0 includes interface skeletons per BP §209 so every later phase is testable
  with fake adapters before hardware/models are required.
- Phase completion is gated by the acceptance criteria in BP §81, §300 and the
  security tests in BP §283-294.
