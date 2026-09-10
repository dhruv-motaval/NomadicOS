# ADR-0004: VisionModel Capability Interface — Gemma First Candidate

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §1.4, §10, §209 (Q4, F4)
- **Source:** Owner answers 2026-09-04

## Decision

Use a dedicated **VisionModel capability interface**. A Gemma-family local vision model
is the first implementation candidate. A multimodal `LocalModel` may satisfy `VisionModel`.

```text
LocalModel
   |
   +---- text generation
   |
   +---- VisionModel capability
```

## Consequences

- Vision implementation is replaceable; Gemma is never hard-coded into the Agent Runtime.
- The registry stores vision capability; the ModelSelector treats vision as a
  required-capability filter like any other.
- This resolves BP §209 (separate `VisionModel`) vs BP §8.1 (embedded
  `generate_with_images`): both exist, linked by capability satisfaction, not duplication.
