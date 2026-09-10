# ADR-0005: Model Acquisition & Supply-Chain Integrity

- **Status:** Accepted
- **Date:** 2026-09-04
- **Resolves:** BP §150-151 (Q5)
- **Source:** Owner answers 2026-09-04

## Decision

Use Hugging Face / direct publisher sources initially. Require: source metadata, version,
checksum/hash verification where available, local registration, and model validation
before activation.

## Consequences

- Model files are a supply-chain boundary; artifacts are untrusted until validated
  (BP §151).
- Model-provided code is never executed automatically.
- Download flow follows BP §150: download → verify source → verify checksum →
  store locally → register. No user data is sent during acquisition.
