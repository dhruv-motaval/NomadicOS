# ADR-0022: Legacy Builds — Archived to `_legacy/`, Excluded from Indexing

- **Status:** Accepted (executed 2026-09-04)
- **Date:** 2026-09-04
- **Resolves:** BP §1.1, §82, §370 (Q22)
- **Source:** Owner answers 2026-09-04

## Decision

Do **not** delete the legacy builds immediately. Archive them outside the active
architecture under `_legacy/` and exclude them from normal agent indexing.

## Executed moves

```text
_legacy/
  input/          (Input Layer: chat/voice/cli + normalizer)
  models/         (Model Intelligence & Provider Layer incl. OmniRouter)
  planner/        (Planner & Intent Router)
  tests/          (their test suites)
  config/         (model_policy.yaml, planner_policy.yaml)
  main.py
  specs/
    NomadicOS — Input Layer.md
    NomadicOS — Model Intelligence & Provider Layer.md
    NomadicOS — Planner & Intent Router Complete Implementation Guide.md
```

## Consequences

- The legacy architecture (OmniRouter, external providers, planner-as-layer) is
  obsolete per BP §1.1, §370. **Never import legacy architecture into the new system.**
- Useful *patterns* may be inspected before porting: Pydantic validation
  (`extra="forbid"`/frozen boundary models), fail-closed behavior, security-test
  style (static sink scans, injection fixtures), benchmark structures.
- `_legacy/` is listed in `.kilocodeignore` so coding agents do not index it.
- A future commit may delete the archive once the new architecture reaches parity;
  deletion requires explicit owner action.
