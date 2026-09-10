# AGENTS.md — NomadicOS working contract

## Canonical specification

`NomadicOS_v0.1_Canonical_Blueprint.md` is the **single source of truth** (cite sections
as `BP §NNN`; never restate it). Confirmed decisions: `docs/architecture/adr/` (ADR-0001…
ADR-0027). Answers of record: `docs/architecture/CANONICAL_ANSWERS_v0.1.md`. Deltas &
errata: `docs/architecture/BLUEPRINT_ADDENDUM_v0.1.md`. **Never silently change an
architectural decision** (BP §372.10) — supersede via a new ADR.

## Non-negotiables (digest — full text in blueprint)

- **Local-only inference.** No external LLM calls exist in v0.1 (BP §1.3, §200, §286).
  The Internet is an information source, never an AI brain.
- **No OmniRouter, no Hermes, no separate Skill System** (BP §1.1, §370). The Agent
  Runtime uses `ModelSelector` (ADR answers Section H).
- **PostgreSQL is canonical** for structured state (BP §1.2, §19); access only via
  repositories/domain services — agents never get raw SQL (BP §93, ADR-0010).
- **Mediated authority:** every effectful action goes
  MODEL → AGENT RUNTIME → TOOL GATEWAY → SECURITY GATE → EXECUTION (BP §73, §363).
  Models propose; they never execute (BP §86, §366).
- **Fail closed** on unknown/invalid security state (BP §85). Invalid policy = fail
  closed (ADR-0013).
- **Open Internet + closed private-data boundary.** Public GETs allowed; no private
  data (files, screenshots, memory, credentials, datasets) to any external AI (Section J).
- **Sessions are context boundaries, not memory boundaries** (BP §376-420). Persistent
  cross-session memory is mandatory (answers Section G).
- **Evidence over claims:** OBSERVE → ACT → VERIFY; "the model said it worked" is not
  proof (BP §366, §82).
- **Self-improvement** is propose → sandbox → benchmark → compare → promote/reject →
  version → monitor → rollback. Never weight changes by default (answers Section I).
- **Legacy builds (`_legacy/`) must never be imported** — patterns only (ADR-0022).
- **No Phase-order shortcuts:** Security Gate (Phase 3) is never skipped to reach
  computer control (ADR-0027, BP §375).

## Phase banner

**CURRENT PHASE: 0 — Repository + tooling + interfaces (not started).**
Phase order (authoritative, ADR-0027): 0 repo/tooling/interfaces → 1 PostgreSQL/core →
2 local model runtime → 3 Security Gate → 4 Tool Gateway/FS/terminal → 5 vision →
6 computer control → 7 browser/network → 8 memory → 9 vector V0 → 10 evaluation →
11 experience → 12 adaptive selection → 13 self-improvement → 14 advanced optimization.
Only touch the current phase's subsystems.

## Commands (Phase 0 target state — ADR-0023)

```bash
uv sync                                       # install deps
docker compose -f docker-compose.dev.yml up -d   # PostgreSQL (ADR-0009; data dirs out of repo/sync)
pytest -m "not hardware"                      # tests (hardware tests marked explicitly, ADR-0024)
ruff check . && ruff format --check .         # lint
mypy src/                                     # types
pre-commit run --all-files                    # hooks
```

## Project layout (target, BP §76)

`src/nomadicos/<subsystem>/` — core · constitution · agent · models · vision · tools ·
security · network · memory · postgres · vector · evaluation · experience · learning ·
audit · backup · extensions · ui. Interfaces before implementations (BP §209); fake
adapters for every hardware-dependent subsystem (analysis §2.2.4).

## Error taxonomy & logging

- Canonical 10-class failure taxonomy: BP §117 (legacy vocabularies map per addendum §3).
- Domain exceptions per BP §84 (PermissionDenied, SecurityPolicyViolation, …), machine-readable context.
- Structured logs (BP §134); centralized redaction of password/token/api_key/… (BP §256);
  never log secrets (BP §134, ADR-0021).

## PR checklist (BP §299 — verify every line before merge)

- [ ] Does this bypass the Security Gate?
- [ ] Does this expose secrets?
- [ ] Does this send data outside?
- [ ] Does this add hidden network access?
- [ ] Does this add an external model dependency?
- [ ] Does this break PostgreSQL authority?
- [ ] Does this break rollback?
- [ ] Does this introduce unbounded loops?
- [ ] Are tests included (security tests for every effectful capability, BP §372.7)?
- [ ] Architecture docs updated if needed (BP §298)?
