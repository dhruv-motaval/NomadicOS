# NomadicOS

**A local-first autonomous AI operating environment.** NomadicOS runs AI agents
on your own machine, using your own local models (Ollama, llama.cpp) — with a
security gate on every action, deterministic verification of every outcome,
persistent cross-session memory, and evidence-based self-improvement.

> **Your machine. Your models. Your data.** No cloud inference. No per-token
> bills. No silent actions.

---

## What it does

NomadicOS executes tasks on your computer — files, terminal, browser — through
a strictly mediated pipeline:

```text
MODEL (proposes) → AGENT RUNTIME → TOOL GATEWAY → SECURITY GATE → EXECUTION
```

Models never execute anything themselves. Every effectful action is authorized
by a policy engine, executed through a tool gateway, and **verified by
evidence** (file hashes, exit codes, screen observation) — never by the
model's own claims.

Core capabilities:

- **Local-only inference** — Ollama / llama.cpp models; zero data leaves the machine
- **Security Gate** — allow / ask / deny per tool, fail-closed by default
- **Tool layer** — filesystem, terminal, web (public GET only), extensible
- **Vision** — multimodal local models read screenshots to ground actions
- **Computer control** — structured keyboard/mouse actions, screen-verified
- **Persistent memory** — cross-session, scoped (project/user/system), auditable
- **Native vector engine** — exact-search baseline, embedding-model versioned
- **Adaptive model selection** — learns which model works for which task family
- **Self-improvement** — propose → sandbox → benchmark → promote/reject → rollback
- **Full audit trail** — append-only, never model-controlled

## Architecture

```text
┌──────────────┐   ┌──────────────┐
│  Chat / CLI  │   │ Vision / I/O │
└──────┬───────┘   └──────┬───────┘
       ▼                  ▼
┌─────────────────────────────────────┐
│           AGENT RUNTIME             │  ← budgets, verification, memory
└──────┬───────────────┬──────────────┘
       ▼               ▼
┌──────────────┐  ┌──────────────┐
│ MODEL SELECT │  │ TOOL GATEWAY │
└──────────────┘  └──────┬───────┘
                         ▼
                ┌─────────────────┐
                │  SECURITY GATE  │  ← policies · permissions · audit
                └────────┬────────┘
                         ▼
                   ┌───────────┐
                   │ EXECUTION │
                   └───────────┘
```

Stack: Python 3.12 · Pydantic · PostgreSQL · Ollama/llama.cpp · asyncio.
~80 source modules, 243 tests (unit + integration + security), ruff + mypy clean.

## Status

v0.1.0 — architecture complete, all 14 build phases verified, running on real
local models (Ollama fleet). See `docs/architecture/` for the canonical
blueprint, 29 architecture decision records, and the dependency license audit.

## Repository layout

```text
src/nomadicos/
  core/        config · events · lifecycle · logging · errors
  constitution/ invariants · policy engine (fail-closed)
  agent/       runtime loop · adaptive model selector
  models/      LocalModel · Ollama · llama.cpp · registry/manager
  security/    security gate · permissions · budgets · data classifier
  tools/       gateway · filesystem · terminal
  vision/      capture → local multimodal → structured observation
  computer/    screen-verified control (Windows-first)
  network/     mediated public-web gateway (GET only)
  memory/      cross-session scoped memory engine
  vector/      native exact-search engine (+ benchmark matrix)
  evaluation/  deterministic verification adapters
  experience/  quality-scored task experience records
  learning/    self-improvement lifecycle with rollback
  audit/ postgres/ backup/ ui/
```

## License

Proprietary — all rights reserved. Private use and portfolio demonstration
permitted; redistribution and commercial use by written agreement only.
See `LICENSE`.
