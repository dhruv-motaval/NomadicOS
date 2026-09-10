# ADR-0026: Specification Gap Resolutions (F1–F7)

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** Analysis report §1.F (F1–F7)
- **Source:** Owner answers 2026-09-04

## F1 — Blueprint formatting typo (BP §274)

Fixed in place: ` ```textquality ` → ` ```text ` + `quality` as the first list item.
The canonical blueprint file was corrected directly.

## F2 — UI timing

CLI is the first user-facing control surface. Local web UI comes later. Logs + CLI are
sufficient for early development. (Also ADR-0014.)

## F3 — pgvector

Allowed only as an experimental benchmark/reference backend. It is **not** the canonical
NomadicOS vector backend. PostgreSQL remains the canonical structured database. (Also ADR-0007.)

## F4 — Vision interface

Use the `VisionModel` capability abstraction. A multimodal `LocalModel` may implement
`VisionModel`. Do not maintain two unrelated model abstractions. (Also ADR-0004.)

## F5 — Session identity on Task

Add `session_id` to the Task object. Canonical trace identity chain:

```text
user_id → session_id → task_id → run_id → step_id
```

Sessions are context containers, **not** memory boundaries (BP §376-420, §399).
Erratum recorded in the blueprint addendum (BP §6.3 / §378 field lists).

## F6 — Startup modes × approval modes precedence

Define an explicit precedence matrix:

```text
SAFE_MODE      → forces stricter approvals
FULL_AUTONOMY  → still cannot override immutable security invariants

Security invariants > owner policy > task policy > agent/model preference
```

(BP §132, §183, §262.)

## F7 — Failure taxonomy

Adopt the canonical 10-class taxonomy (BP §117):

```text
MODEL_FAILURE, TOOL_FAILURE, VISION_FAILURE, NETWORK_FAILURE, PERMISSION_FAILURE,
RESOURCE_FAILURE, ENVIRONMENT_FAILURE, PLANNING_FAILURE, VERIFICATION_FAILURE,
UNKNOWN_FAILURE
```

Legacy classification vocabularies from superseded builds map into these classes
(mapping table in `BLUEPRINT_ADDENDUM_v0.1.md`).
