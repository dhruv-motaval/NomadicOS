# ADR-0002: Hardware Profile — Adaptive, No Hard GPU Requirement

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §53, §170 (Q2)
- **Source:** Owner answers 2026-09-04

## Decision

Do not define a hard single-GPU requirement. Support CPU fallback, GPU acceleration when
available, and dynamic RAM/VRAM detection.

A **HardwareProfile** subsystem reports: CPU, RAM, GPU, VRAM, architecture, OS.
Model selection consumes this profile (BP §170).

## Consequences

- No promise that every model runs on every machine; candidates whose resource
  requirements exceed the profile are filtered out by the ModelSelector (BP §53).
- The profile is discovered at startup / first run (BP §324) and stored with policy permission.
