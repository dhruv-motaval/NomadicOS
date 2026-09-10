# Architecture Decision Records — Index

Canonical specification: `NomadicOS_v0.1_Canonical_Blueprint.md` (cite as `BP §NNN`).
Resolutions source: `docs/architecture/CANONICAL_ANSWERS_v0.1.md` (owner answers, 2026-09-04,
via GPT-5.6 Luna review). Blueprint deltas: `docs/architecture/BLUEPRINT_ADDENDUM_v0.1.md`.

| ADR | Decision | Resolves |
|---|---|---|
| [0001](ADR-0001-local-inference-runtime.md) | `LocalModel` interface first; llama-cpp-python as first adapter (spike first) | Q1 |
| [0002](ADR-0002-hardware-profile.md) | HardwareProfile subsystem; CPU fallback + GPU when available; no hard GPU floor | Q2 |
| [0003](ADR-0003-embedding-model-baseline.md) | Configurable/versioned local embeddings; ~384-dim baseline; locked after retrieval benchmark | Q3 |
| [0004](ADR-0004-visionmodel-capability-interface.md) | `VisionModel` capability interface; Gemma first candidate; multimodal LocalModel may satisfy it | Q4, F4 |
| [0005](ADR-0005-model-acquisition-integrity.md) | HF/publisher acquisition; checksum verification; models are untrusted artifacts | Q5 |
| [0006](ADR-0006-sequential-model-residency.md) | Sequential model residency default; concurrent only if resources permit | Q6 |
| [0007](ADR-0007-vector-exact-search-first.md) | Own exact-search vector baseline first; external backends experimental only | Q7 |
| [0008](ADR-0008-vector-storage-layout.md) | `NOMADICOS_DATA_DIR/data/vector/<index-id>/`; versioned format metadata | Q8 |
| [0009](ADR-0009-postgres-docker-dev.md) | Docker Compose for dev; setup abstraction later; PG data out of repo/sync dirs | Q9 |
| [0010](ADR-0010-psycopg3-driver.md) | psycopg 3; repositories/domain services only; no arbitrary SQL for agents | Q10 |
| [0011](ADR-0011-backup-strategy.md) | pg_dump logical backups; separate vector snapshots; tested restores | Q11 |
| [0012](ADR-0012-process-model.md) | Single asyncio process; process-execution abstraction for future isolation | Q12 |
| [0013](ADR-0013-constitution-two-layers.md) | Immutable invariants in code + schema-validated versioned YAML policies | Q13 |
| [0014](ADR-0014-cli-first-ui.md) | CLI-first UI; local web dashboard after core runtime; HTTP API optional/authenticated | Q14, F2 |
| [0015](ADR-0015-cli-task-intake.md) | `nomadicos task create "..."` talks to the same runtime interface as the future UI | Q15 |
| [0016](ADR-0016-watchdog-emergency-stop.md) | In-process watchdog; out-of-band emergency stop; budgets enforced outside model | Q16 |
| [0017](ADR-0017-inprocess-scheduler.md) | In-process priority scheduler (foreground > learning > benchmark > backup > maintenance) | Q17 |
| [0018](ADR-0018-windows-sandbox-layered.md) | Layered Windows restrictions; Sandbox abstraction stronger than implementation | Q18 |
| [0019](ADR-0019-deterministic-data-classifier.md) | Deterministic-first classifier; uncertain ⇒ stricter | Q19 |
| [0020](ADR-0020-audit-integrity.md) | Append-only audit + file mirror + optional hash-chain digest | Q20 |
| [0021](ADR-0021-secret-store.md) | Windows Credential Manager/DPAPI + encrypted fallback; no plaintext secrets anywhere | Q21 |
| [0022](ADR-0022-legacy-archived.md) | Legacy builds archived to `_legacy/`, excluded from agent indexing; never imported | Q22 |
| [0023](ADR-0023-toolchain.md) | Python 3.12 · uv · ruff · mypy · pytest · pre-commit | Q23 |
| [0024](ADR-0024-ci-strategy.md) | Local checks + GitHub Actions; security regression fails CI | Q24 |
| [0025](ADR-0025-license-packaging.md) | License deferred to dependency audit; PowerShell bootstrap for Windows v0.1 | Q25 |
| [0026](ADR-0026-gap-resolutions-F1-F7.md) | Gap resolutions F1–F7 (typo fix, pgvector scope, session_id, precedence, taxonomy) | F1–F7 |
| [0027](ADR-0027-phase-order.md) | Authoritative Phase 0–14 order; Security Gate at Phase 3; never skipped | K |
| [0028](ADR-0028-license-proprietary.md) | Proprietary license with portfolio-share + commercial-rights-reserved clauses | ADR-0025 open item |
| [0029](ADR-0029-product-distribution.md) | NomadicOS will be sold as a product; packaging roadmap opened (PRODUCT_ROADMAP.md) | Owner decision |

## Standing rules for new ADRs

- One decision per ADR; include BP section refs, the owner-question it resolves, and consequences.
- Never silently change an architectural decision (BP §372.10); supersede with a new ADR instead.
- Any major architectural change updates architecture docs, security docs, tests, and the
  migration plan (BP §298).
