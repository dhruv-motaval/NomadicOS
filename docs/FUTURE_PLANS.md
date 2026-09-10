# NomadicOS — Future Plans & Ideas (NOT decisions)

> Status: informal planning notes only. Nothing in this file changes the architecture.
> Canonical decisions live in `docs/architecture/adr/` (ADR-0001…ADR-0031) and the
> blueprint. When any idea below becomes real, it gets its own ADR first
> (BP §372.10 — never silently change architecture).
>
> Current focus: make v0.1 run end-to-end and stable. Everything here is "later."

---

## 1. Service Layer + Rust CLI (the "native app" upgrade)

### Why (decided after profiling discussion)

- NomadicOS spends ~90% of wall-clock waiting on the LLM (Ollama). A Rust rewrite
  of the core would gain ~1–3% end-to-end while destroying the verified trust
  layer (274 tests, security gate, audit). Rejected.
- The CLI, however, is just a face: menus + requests. Rewriting **only the CLI**
  in Rust buys instant startup (~10 ms vs ~400 ms) and a single-file `.exe`
  distribution with no Python install needed.

### Locked-in decisions for that upgrade (to implement via ADR-0032)

| Topic | Decision |
|---|---|
| Runtime core | Python (unchanged) |
| Service layer | FastAPI + asyncio, wraps existing Runtime |
| Transport (v1) | HTTP on 127.0.0.1 (named pipes/UDS only if ever justified by profiling) |
| Streaming | SSE (step events, reconnect with replay) |
| CLI v1 | Rust: clap + crossterm REPL (no Ratatui in v1) |
| Auth | Bearer token on every endpoint, token stored per BP §256 / ADR-0021 |
| TUI upgrade | Ratatui only in a later version |

### API v1 (six endpoints, locked)

- `POST /v1/goals` — single entry; runtime decides chat vs task itself.
  Supports `Idempotency-Key: <uuid>` header so CLI retries never double-execute
  (store key on tasks row — migration 004 — plus startup sweep that fails stale
  RUNNING tasks after a crash).
- `GET /v1/goals/{id}` — report status.
- `GET /v1/events?goal={id}` — SSE step events with Last-Event-ID replay.
- `POST /v1/memory/search`
- `GET /v1/status`
- `POST /v1/emergency-stop` — first-class endpoint (BP §122), works even while a
  goal is streaming.

### Build order (when we start)

1. `git init` + first commit (repo has no version control yet!)
2. ADR-0032: service contract + auth + transport abstraction
3. `openapi.yaml` (the contract both sides depend on)
4. FastAPI wrapper at `src/nomadicos/api/` (BP §76 subsystem pattern)
5. SSE event stream (per-goal fan-out + ring buffer for reconnect replay)
6. Rust CLI v1 (`cli-rs/`): clap + crossterm REPL + reqwest — client depends
   only on the OpenAPI contract, never on Python modules
7. Migrate CLI tests to API contract tests

### Scope guard

v1 is ~1–2k lines of Rust + ~500–800 lines of Python. Ratatui, named pipes,
dashboard, multi-transport — all v2. The lesson from FotoOwl applies to us too:
don't let the shiny part eat the project.

---

## 2. The Body — Brain + Agents as Organs (raw idea notes, plan later)

Owner vision: NomadicOS as one **Brain** with **agents as body parts**. Rough
mapping to what already exists vs what doesn't (nothing below is committed):

- **Brain** — the orchestrator/planner core. Exists partially (planner → DAG →
  waves); idea: one central "thinking" agent that owns all coordination, memory
  recall, and decision-making, and delegates everything else.
- **Hands** — computer control + terminal + filesystem tools. Exists (Phase 4/6,
  Windows adapter hardware-marked). Idea: finer "hand" agents for specific grips
  (typing hand, clicking hand, dragging hand).
- **Eyes** — vision model (gemma3:4b multimodal). Exists (Phase 5). Idea:
  continuous ambient perception, not just step-boundary captures — eyes that
  notice screen changes on their own.
- **Ears** — speech input (Whisper). Exists in projects, not wired into
  NomadicOS. Idea: voice intake as a first-class input layer.
- **Mouth** — voice output (ElevenLabs/TTS). Exists in projects (iWebwala),
  not in NomadicOS. Idea: the agent speaks its reports.
- **Legs / movement** — navigation: browser automation, window switching,
  multi-app movement. Phase 7 partial (network exists, browser control doesn't).
- **Memory (hippocampus)** — cross-session memory engine + experience records.
  Exists (Phases 8–9). Idea: consolidation "during sleep" — a background
  process that compresses the day's experiences into procedures.
- **Immune system** — Security Gate + invariants + audit. Exists (Phase 3).
  Idea: anomaly detection on the agent's own behavior ("this action doesn't
  look like me").
- **Nervous system** — event bus + trace contexts. Exists (Phase 0).

Open questions for later planning: does the Brain pick organs per subtask, or do
organs self-register capabilities? How do hands and eyes share one visual
workspace? Latency budgets per organ? All unplanned — brainstorm when v0.1 runs.

---

## 3. Misc backlog (small, unprioritized)

- Make the NomadicOS repo public with a real README (architecture diagram, demo
  GIF) — currently the biggest credibility gap for job applications.
- Regenerate `portfolio/src/App.jsx` data automatically from `src/nomadicos/`
  + ADR index so stats (tests/ADRs/modules) can't drift.
- Vector engine hot path in Rust via PyO3 — only if benchmarks ever show
  retrieval matters (10k+ vectors). Until then: no.
- Wire the FastAPI dashboard (ADR-0014) after the service layer exists — same
  API, third client.
