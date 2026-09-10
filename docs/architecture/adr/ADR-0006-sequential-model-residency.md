# ADR-0006: Sequential Model Residency Default

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §8.5, §153, §156-157, §187 (Q6)
- **Source:** Owner answers 2026-09-04

## Decision

Sequential model residency is the default v0.1 behavior:

```text
Model A
  |
unload/swap
  |
Model B
```

Multiple simultaneously loaded models are allowed only when hardware/resources permit.
Models are not required to stay loaded.

## Consequences

- The Model Manager exposes: load, unload, health, resource requirements.
- The ModelSelector must consider loading cost (swap latency) in its scoring.
- Protects consumer GPUs from VRAM thrashing (BP §153).
