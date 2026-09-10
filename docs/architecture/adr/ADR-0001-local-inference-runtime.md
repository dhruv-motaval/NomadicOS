# ADR-0001: Local Inference Runtime — llama-cpp-python First

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §8.1, §301 (Q1)
- **Source:** Owner answers 2026-09-04 (GPT-5.6 Luna review of Canonical Blueprint Analysis)

## Decision

Do a short runtime spike first, but make **llama-cpp-python** the preferred first
implementation target for the `LocalModel` adapter.

- Local/in-process execution fits the local-first design.
- Good control over model loading/unloading.
- Suitable for GGUF-style local models.
- Avoids introducing a model server dependency unnecessarily.
- The `LocalModel` interface keeps the runtime replaceable later.

## Consequences

- `LocalModel` is implemented as an abstraction **first**; llama-cpp-python-specific
  code must never spread into the Agent Runtime — it lives only in the adapter.
- Later runtimes (Ollama, LM Studio, vLLM) plug in behind the same interface.
- Phase 2 begins with the interface + fake adapter, then the spike, then the real adapter.
