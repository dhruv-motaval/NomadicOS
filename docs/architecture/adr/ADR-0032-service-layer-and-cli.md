# ADR-0032: Service Layer + Rust CLI (accessibility arc)

- **Status:** Accepted
- **Date:** 2026-09-10
- **Builds on:** ADR-0014 (UI as client), ADR-0015 (CLI), ADR-0027 (phase order), BP §76 (subsystem layout)
- **Owner decision:** make NomadicOS reachable from terminals and CLIs through a local
  service layer, and later a Rust terminal client. The Python core is NOT rewritten
  (measured: ~90% of wall-clock is Ollama inference; orchestration overhead is noise).

## Decision

1. **Service layer:** FastAPI app at `src/nomadicos/api/` wrapping the existing
   `Runtime` in-process. One Runtime instance per service process.
2. **Transport (v1):** HTTP on 127.0.0.1 only. Named pipes/UDS are explicitly
   deferred — measured benefit is sub-millisecond per call against multi-second
   LLM calls; the client keeps transport swappable behind its own interface.
3. **Auth:** Bearer token on every endpoint. Token auto-generated at first boot,
   stored in `data/api-token` (user-only file, never logged — I12, BP §256).
4. **API v1 (six endpoints):**
   - `POST /v1/goals` — single entry point; the Runtime decides chat vs task
     (conversational routing already exists in the agent loop). Accepts
     `Idempotency-Key` header; duplicate keys return the existing goal.
   - `GET /v1/goals/{goal_id}` — status + report when finished.
   - `GET /v1/events/{goal_id}` — SSE stream of goal state.
   - `POST /v1/memory/search`
   - `GET /v1/status`
   - `POST /v1/emergency-stop` — first-class, works during streaming (BP §122).
5. **Goals run in background tasks** inside the service; the registry is
   in-memory for v1 (goal_id → status/report). Persistence of the registry is a
   later decision; tasks/audit already persist via PostgreSQL.
6. **Rust CLI (later arc):** clap + crossterm REPL + reqwest; depends only on the
   served OpenAPI contract (`/openapi.json` — the contract is generated, not
   hand-maintained, so it cannot drift), never on Python modules.

## Alternatives considered

- **Full Rust port** — rejected: destroys the tested trust layer for ~1–3%
  measured gain; the bottleneck is Ollama inference, not Python.
- **Named pipes/UDS first** — rejected for v1: uvicorn/ASGI has no Windows named
  pipe support; complexity before any working transport.
- **stdin/stdout RPC** — rejected: weak for streaming and multiple clients.
- **Hand-written openapi.yaml** — rejected: drifts from code; FastAPI serves the
  contract from the implementation.

## Consequences

- NomadicOS becomes scriptable from any terminal (`curl`), by scripts, and later
  by a native Rust binary.
- The in-process CLI remains fully functional during and after the transition.
- API tests live in `tests/unit/api/` and exercise the contract with a fake
  Runtime (no models, no network).
