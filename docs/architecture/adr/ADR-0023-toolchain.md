# ADR-0023: Toolchain — Python 3.12, uv, ruff, mypy, pytest, pre-commit

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §83, §301-302 (Q23)
- **Source:** Owner answers 2026-09-04

## Decision

```text
Python:            3.12
Package manager:   uv
Formatter/linter:  ruff
Type checking:     mypy
Testing:           pytest
Pre-commit:        yes
```

## Consequences

- Keep the toolchain boring and deterministic (owner directive).
- `pyproject.toml`, lockfile, ruff/mypy configs, and pre-commit hooks land in Phase 0
  (BP §77 Phase 0: configuration, logging, error handling, dependency management,
  test framework).
- Coding standards from BP §83 are enforced by these tools where automatable
  (typed interfaces, structured logging, explicit exception handling).
