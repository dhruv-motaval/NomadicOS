# NomadicOS — Interactive Architecture Explorer

**One file. Zero setup. Open `nomadicos-architecture.html` in any browser and
click around — everything works offline.**

## What you are looking at

This bundle documents **NomadicOS v0.1** — a local-first autonomous AI
operating environment: AI agents that execute real tasks on a computer
(files, terminal, browser) while every single action passes through a
security gate, gets verified with hard evidence, and is fully reversible.

No cloud inference. No per-token bills. No data leaving the machine.

## Files in this folder

| File | What it is |
|---|---|
| **`nomadicos-architecture.html`** | The interactive map — 29 components across 10 layers, plus the 14-step task lifecycle flow. Click any node or step for: what it does, how it works, the technology used and why, blueprint references, code paths, and the security invariants it guards. Includes a trust-boundary overlay showing where the Security Gate mediates the model. |
| **`NomadicOS_Canonical_Blueprint.md`** | The complete v0.1 architecture specification (420 sections) — the source of truth every component was built against. |
| **`ADR_INDEX.md`** | 29 Architecture Decision Records — every major design decision, its alternatives, and rationale. |
| **`PRODUCT_ROADMAP.md`** | The packaging & distribution plan for the commercial release. |

## How to navigate the architecture map

1. **Component Graph** (default) — 10 color-coded layers, 27 mediated edges.
   Click any node → the side panel shows its responsibilities, mechanism,
   technology + rationale, guarded security invariants, and code/test paths.
2. **Task Lifecycle Flow** — the 14 steps every goal travels:
   intake → policy → memory recall → model selection → residency →
   structured proposal → schema validation → budgets → **Security Gate** →
   execution → evidence verification → experience → memory → truthful report.
3. **"Show trust boundary"** — highlights the core chain:
   `MODEL (proposes) → AGENT RUNTIME → TOOL GATEWAY → SECURITY GATE → EXECUTION`
4. **Layer filter** — isolate any subsystem (Security, Model Intelligence,
   Knowledge & Learning, …).

## The security model in one paragraph

Models propose; they never execute. Every effectful action is authorized by a
fail-closed policy engine, budgeted outside the model, executed through a tool
gateway with schema-validated arguments, verified with deterministic evidence
(file hashes, exit codes — never the model's own claims), recorded in an
append-only audit trail enforced at the database level, and reversible via
versioned self-improvement with automatic rollback. Sessions are context
boundaries, not memory boundaries — the system learns across sessions, but
nothing ever leaves the machine.

## Verified state (as shipped)

- 243 automated tests (unit + integration + security) — green
- ruff + mypy clean across 87 source files
- Real end-to-end run: local model proposes → security gate authorizes →
  file written → SHA-256 evidence verified → experience recorded
- Live fleet self-adaptation proven: models added/removed/updated on the
  model server are detected and adopted without restart

## Build numbers

- 14 development phases (ADR-0027)
- 29 Architecture Decision Records
- 82 Python source modules, 87 files type-checked
- 243 tests + 3 migrations + 5 model integrations
