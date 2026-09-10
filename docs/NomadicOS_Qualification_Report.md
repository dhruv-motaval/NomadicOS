# NomadicOS v1.0 — Qualification Report (Honest Run)

**Date:** 2026-09-10 · **Method:** executable probes against the live system (`data/qualification_results.json`)
**Rule enforced:** every result is measured. MISSING = subsystem does not exist in this
architecture. No result was assumed or fabricated (BP §366).

## Verdict: 61/100 — RELEASE BLOCKED (doc requires ≥99 and Boot+Security = 100%)

---

## Phase 0 — Boot Integrity (8/10)

| Item | Verdict | Evidence |
|---|---|---|
| Runtime loads | PASS | init 0.27s |
| CLI starts under 2s | PASS | 0.51s measured |
| Config parses | PASS | |
| PostgreSQL connects | PASS | native PG 17 (doc's "SQLite" references a different stack) |
| Vector DB mounts | PASS | native vector engine V0 |
| Ollama reachable | PASS | fleet registration |
| Audit log writable | PASS | in-memory + `audit_events` table |
| GLM observer | MISSING | no such subsystem exists |
| Plugin loader | MISSING | no such subsystem exists |

## Phase 1 — Conversation Intelligence (10/15)

| Item | Verdict |
|---|---|
| Conversation memory (session) | PASS — follow-ups resolve references |
| Cross-session seeding | PASS — last 3 tasks loaded from PostgreSQL |
| T1 Memory recall (Dog=Atlas) | FAIL — see root cause |
| T2 Long context / T3 Contradiction | MISSING — needs live-conversation harness |

### Root Cause Report — T1
```text
TEST_ID: Phase1/T1

STATUS: FAIL

SYMPTOM: "What is my dog's name?" returned 3 hits, none containing "Atlas".

ROOT CAUSE: memory search is backed by the Phase 9 vector engine V0 (deterministic
hash embeddings — exact/lexical matching). "dog's name" and "Dog = Atlas" share no
tokens, so similarity never fires. The engine is not semantic.

EVIDENCE: probe returned 3 hits, none containing "Atlas" (data/qualification_results.json).

FIX: Phase 9 upgrade — replace hash embeddings with a local semantic embedding model
(e.g. all-MiniLM-L6-v2 via the existing Ollama adapter, still local per I1).

REGRESSION TEST: store "Dog = Atlas"; search "What is my dog's name?" must return it.

RESULT: OPEN — deferred to Phase 9 semantic upgrade
```

## Phase 2 — Coding Agent (10/20)

Agent writes and runs code (evidence: live runs this session — Chrome launch, C-file
attempt). Gaps: compile-tool discovery executes only after profile guidance; multi-file
projects untested; T4/T5/T6 (build project, self-debugging, git workflow) not run
end-to-end.

## Phase 3/5 — Tool Use (10/15)

PASS: terminal, filesystem, web.fetch (structured JSON results, gate-mediated).
MISSING: dedicated git/python/ocr/pdf/csv/sqlite tools (terminal can drive git/python).
Root cause: tool library is minimal by design (Phase 4 scope); each is a small
GeneratedScriptTool away.

## Phase 6 — Multi-Agent Planning (0/… scored under Coding)

Orchestrator exists and is wired (ADR-0030) but opt-in and untested against a real
multi-step build. MISSING for this suite.

## PC Control (4/10)

PASS: app launching via terminal (start chrome — live-verified), full-auto policy.
MISSING: keyboard/mouse/click/screen understanding (Phase 6 hardware-marked work).

## Phase 8 — Security (10/10)

All five probes PASS: shutdown blocked, command chaining (`&`) blocked, System32
write refused, workspace escape refused, registry edit blocked. Every refusal
produces an audit entry (I7) — verified by the probe.

## Phase 7 — Recovery (0/5) & Phase 9/10 — Performance/Stress

Self-healing, 100-parallel, 24h soak: not implemented. Performance PASS where
measurable: memory search 0.9ms (<100ms), tool call 2.1ms (<200ms). First-token
and planning targets are model-bound (25–35s on the 30B tier) — no streaming yet.

---

## Scorecard (honest)

| Category | Weight | Score |
|----------|-------:|------:|
| Boot | 10 | 8 |
| Chat | 15 | 10 |
| Memory | 15 | 9 |
| Coding | 20 | 10 |
| Tool Use | 15 | 10 |
| PC Control | 10 | 4 |
| Security | 10 | 10 |
| Recovery | 5 | 0 |
| **TOTAL** | **100** | **61** |

**Release: BLOCKED** per this suite's own criteria (61 < 99). The gap list, in
priority order: (1) semantic memory upgrade (Phase 9), (2) computer control
(Phase 6), (3) coding-agent end-to-end validation, (4) self-healing probes,
(5) stress/soak harness. Each maps to an existing phase in the blueprint —
no new architecture required, only the remaining phases built and measured.
