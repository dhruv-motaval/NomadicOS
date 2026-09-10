# ADR-0025: License & Packaging — License Deferred to Audit; PowerShell Bootstrap

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §76, §130 (Q25)
- **Source:** Owner answers 2026-09-04

## Decision

Do **not** finalize the open-source license purely as an engineering assumption.
Perform a dependency/license audit first — license choice is a project/legal decision.

For Windows v0.1, a PowerShell bootstrap/setup script is acceptable packaging.

## Consequences

- No LICENSE file until the audit completes; dependency choices must be recorded with
  their licenses as they land (BP §313: respect licenses of studied projects).
- Packaging (BP §130: installer/setup script, dependency checks, PostgreSQL setup,
  model setup, configuration wizard, health check) starts with the PowerShell bootstrap
  and grows into the setup abstraction over time (ADR-0009).
