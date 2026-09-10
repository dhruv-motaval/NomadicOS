# ADR-0024: CI — Local Checks + GitHub Actions

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §191-192, §294 (Q24)
- **Source:** Owner answers 2026-09-04

## Decision

Use **both**: local checks + GitHub Actions.

Minimum CI:

- unit tests
- integration tests
- security tests
- static checks (ruff)
- type checks (mypy)

Hardware-dependent tests are explicitly marked (pytest marker, e.g. `@pytest.mark.hardware`)
and excluded from shared CI runs.

## Consequences

- **A security regression must fail CI** (BP §294).
- The security invariant suite (BP §191) and red-team suite (BP §192) run in CI from
  Phase 0 onward.
- Local-first architecture does not mean avoiding repository CI (owner directive).
