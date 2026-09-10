# ADR-0019: Data Classifier — Deterministic First

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §264-266 (Q19)
- **Source:** Owner answers 2026-09-04

## Decision

Deterministic-first classification. Initial techniques: path heuristics, regex,
file extensions, known secret patterns, entropy, explicit user classification.
Model-based classification can be added later.

Rule: **uncertain → stricter classification** (BP §266).

## Consequences

- Classification is auditable and conservative (BP §265: probabilistic and conservative).
- Detected classes: credentials, PII, financial identifiers, private project data,
  public docs (BP §265).
- Feeds the Security Gate's data-classification check (BP §12, §36.1) and file
  content redaction (BP §163).
