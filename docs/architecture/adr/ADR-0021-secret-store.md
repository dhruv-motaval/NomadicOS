# ADR-0021: Secret Store — Windows Credential Manager / DPAPI + Encrypted Fallback

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §40, §103, §256, §271 (Q21)
- **Source:** Owner answers 2026-09-04

## Decision

Use **Windows Credential Manager / DPAPI** behind the `SecretManager` abstraction.
Fallback: encrypted local store for portability. Use native OS security first.

## Consequences

- Plaintext secrets must never appear in: logs, audit, memory, prompts, experience
  records, or model benchmark traces (BP §256 redaction applies everywhere).
- Environment variables carry only deployment-level settings (BP §103); they are not
  the secret store.
- The future Credential Broker (BP §271) can sit on top of this store without
  changing the model-facing interface.
