# ADR-0013: Constitution — Code Invariants + Versioned YAML Policies

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §4, §190, §257-258, §262 (Q13)
- **Source:** Owner answers 2026-09-04

## Decision

Two layers:

1. **Immutable security invariants in code** (`constitution/invariants.py`) — enforced
   structurally and by the invariant test suite (BP §191).
2. **Versioned YAML policies for user-configurable behavior** (`config/policies/*.yaml`),
   validated by `constitution/policy_schema.py` and loaded by `constitution/policy_loader.py`.

```text
constitution/
  invariants.py
  policy_schema.py
  policy_loader.py

config/policies/*.yaml
```

## Consequences

- The model can never rewrite the security foundation (BP §190, §260).
- Policy files are schema-validated; **invalid policy = fail closed** (BP §85, §257).
- Policy precedence follows BP §262: immutable invariants > owner policy > task policy >
  agent/model preference > external content. Startup-vs-approval interaction matrix per ADR-0023.
