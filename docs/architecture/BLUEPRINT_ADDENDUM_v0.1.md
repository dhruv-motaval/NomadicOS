# Blueprint Addendum v0.1 — Confirmed Decisions & Errata

> **Status:** Authoritative supplement to `NomadicOS_v0.1_Canonical_Blueprint.md`.
> The blueprint remains the canonical spec; this addendum records *confirmed decisions*,
> *clarifications*, and *errata* resolved on 2026-09-04. Canonical answers:
> `docs/architecture/CANONICAL_ANSWERS_v0.1.md`. Decision records:
> `docs/architecture/adr/README.md` (ADR-0001 … ADR-0027).

## 1. Confirmed decisions (map to ADRs)

| Topic | Decision | BP refs | ADR |
|---|---|---|---|
| Local inference | `LocalModel` interface first; llama-cpp-python first adapter (short spike first) | §8.1, §301 | 0001 |
| Hardware | HardwareProfile subsystem; CPU fallback; no hard GPU floor | §53, §170 | 0002 |
| Embeddings | Configurable/versioned; ~384-dim baseline; final choice after retrieval benchmark | §345-347, §107 | 0003 |
| Vision | `VisionModel` capability interface; Gemma first candidate | §1.4, §10, §209 | 0004 |
| Model acquisition | HF/publisher sources; checksum verification; artifacts untrusted until validated | §150-151 | 0005 |
| Model residency | Sequential default; concurrent only if resources permit | §8.5, §153 | 0006 |
| Vector V0 | Own exact-search baseline first; external backends experimental only | §20-22, §303-305 | 0007 |
| Vector storage | `NOMADICOS_DATA_DIR/data/vector/<index-id>/`; versioned format metadata | §21, §311 | 0008 |
| PostgreSQL deploy | Docker Compose (dev); setup abstraction later | §130, §173 | 0009 |
| PG driver | psycopg 3; repositories only; no arbitrary SQL for agents | §93 | 0010 |
| Backups | pg_dump + separate vector snapshots; tested restores | §43, §105 | 0011 |
| Process model | Single asyncio process; process-execution abstraction for future isolation | §91, §129 | 0012 |
| Constitution | Immutable invariants in code + schema-validated versioned YAML policies | §4, §190, §257 | 0013 |
| UI/API | CLI-first; web dashboard later; HTTP API optional and authenticated if added | §54-55, §127 | 0014 |
| Task intake | `nomadicos task create "..."` via the runtime interface | §78 | 0015 |
| Watchdog/stop | In-process watchdog; out-of-band emergency stop | §70, §121-122 | 0016 |
| Scheduler | In-process priority scheduler; asyncio + budgets | §244-249 | 0017 |
| Sandbox | Layered Windows restrictions; abstraction stronger than implementation | §35, §91 | 0018 |
| Data classifier | Deterministic-first; uncertain ⇒ stricter | §264-266 | 0019 |
| Audit integrity | Append-only table + file mirror + optional hash-chain digest | §41-42, §251 | 0020 |
| Secrets | Windows Credential Manager/DPAPI + encrypted fallback | §40, §103 | 0021 |
| Legacy code | Archived to `_legacy/`; excluded from indexing; never imported | §1.1, §370 | 0022 |
| Toolchain | Python 3.12 · uv · ruff · mypy · pytest · pre-commit | §83, §301 | 0023 |
| CI | Local checks + GitHub Actions; security regression fails CI | §191-192, §294 | 0024 |
| License/packaging | License deferred to dependency audit; PowerShell bootstrap for Windows v0.1 | §76, §130 | 0025 |
| Phase order | Phase 0–14 authoritative (below) | §77, §375 | 0027 |

## 2. Errata & clarifications (F1–F7)

1. **F1 (fixed in BP §274):** ` ```textquality ` → ` ```text ` + `quality`.
2. **F2:** CLI is the first user-facing control surface; logs + CLI suffice for early
   development (§54-55 timing clarified).
3. **F3:** pgvector is permitted only as an experimental benchmark/reference backend
   (§20.1 read together with §20.2); the canonical vector backend is the native engine.
4. **F4:** `VisionModel` is a capability interface (§209); a multimodal `LocalModel`
   (§8.1) may implement it. One model abstraction family, two capability surfaces.
5. **F5:** The Task object (§6.3) carries `session_id`. Canonical trace identity:
   `user_id → session_id → task_id → run_id → step_id` (§254, §399).
6. **F6:** Startup modes (§132) × approval modes (§183) precedence matrix:
   SAFE_MODE forces stricter approvals; FULL_AUTONOMY still cannot override immutable
   security invariants; order per §262.
7. **F7:** Failure taxonomy (§117) is the single canonical 10-class vocabulary; all
   prior/legacy vocabularies map into it (mapping table below).

## 3. Legacy failure taxonomy mapping (per F7)

| Legacy class (superseded builds) | Canonical (§117) |
|---|---|
| PROVIDER_UNAVAILABLE | MODEL_FAILURE (external providers removed in v0.1) |
| MODEL_UNAVAILABLE / TIMEOUT (model) | MODEL_FAILURE |
| INVALID_RESPONSE | MODEL_FAILURE or VERIFICATION_FAILURE (per evidence) |
| AUTHENTICATION_ERROR (external model) | PERMISSION_FAILURE (external inference is blocked in v0.1, §286) |
| CONTEXT_TOO_LARGE | RESOURCE_FAILURE |
| RATE_LIMITED | RESOURCE_FAILURE |
| COST_LIMIT_EXCEEDED | POLICY → PERMISSION_FAILURE (cost policy is owner policy) |
| SECURITY_POLICY_BLOCKED | PERMISSION_FAILURE |
| UNKNOWN | UNKNOWN_FAILURE |
| Tool/network/terminal errors | TOOL_FAILURE / NETWORK_FAILURE / ENVIRONMENT_FAILURE |
| Plan validation errors | PLANNING_FAILURE |

## 4. Authoritative phase order (ADR-0027, owner Section K)

```text
PHASE 0   Repository + tooling + interfaces
PHASE 1   PostgreSQL + core state
PHASE 2   Local model runtime + registry
PHASE 3   Security Gate + policies + permissions + audit
PHASE 4   Tool Gateway + filesystem + terminal
PHASE 5   Vision + Gemma adapter
PHASE 6   Computer control
PHASE 7   Browser + controlled Internet
PHASE 8   Memory Engine
PHASE 9   Native Vector Engine exact-search baseline
PHASE 10  Evaluation + verification
PHASE 11  Experience system
PHASE 12  Adaptive model selection
PHASE 13  Self-improvement + benchmarks
PHASE 14  Advanced vector engine / optimization
```

## 5. Repository status after resolution (2026-09-04)

- Blueprint: `NomadicOS_v0.1_Canonical_Blueprint.md` (canonical, F1 fixed in place).
- Analysis: `NomadicOS_Canonical_Blueprint_Analysis.md` (§1 questions resolved — see
  its RESOLUTION banner).
- Answers of record: `docs/architecture/CANONICAL_ANSWERS_v0.1.md`.
- ADRs: `docs/architecture/adr/` (27 records + index).
- Legacy builds: `_legacy/` (not indexed, never imported; deletion requires explicit
  owner action later).
- No Phase 0 implementation has been started beyond repository documentation and
  legacy archival.
