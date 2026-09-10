# ADR-0015: Task Intake — CLI Task Command

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §78, §121 (Q15)
- **Source:** Owner answers 2026-09-04

## Decision

CLI-first task intake:

```text
nomadicos task create "Open VS Code and run the tests..."
```

## Consequences

- Deterministic, easy to test, easy to automate.
- The task CLI talks to the **same Agent Runtime interface** the future UI will use —
  no separate intake path.
- User override controls (stop/pause/resume/cancel per BP §121) are exposed on the
  same CLI surface from Milestone 1.
