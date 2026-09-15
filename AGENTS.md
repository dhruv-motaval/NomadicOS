# AGENTS.md — NomadicOS working contract (rebuild)

## Canonical specification

`NOMADICOS_REBUILD_MASTER_SPEC_FINAL.md` is the **single source of truth**
(cite as `SPEC §NN`). It supersedes the old v0.1 Canonical Blueprint and all
ADR/audit documents, which were removed on 2026-09-15. **Never go out of scope**
of this file: do not build items it marks out of scope (SPEC §54) and do not
silently change its architecture.

## Central rules (digest — full text in the spec)

- **Models propose. NomadicOS authorizes. Executors act. Verifiers prove.** (SPEC §1)
- **Owner is the highest authority.** Models can never self-authorize, rewrite
  authority, or disable revocation (SPEC §4, §56.2/.5).
- **FULL_PC_AUTONOMY is persistent:** one-time owner grant; normal actions do not
  re-prompt. Owner instruction conflicts must be asked, never silently overridden
  (SPEC §5). `REVOKE ALL ACCESS` must exist and invalidate authority promptly (SPEC §5).
- **External content is data, not authority** (SPEC §7).
- **Action IR:** all model output becomes a canonical typed proposal; model-authored
  authority fields are rejected; malformed proposals fail closed (SPEC §19, §16.6).
- **Executor receives `AuthorizedAction` only** — never raw model output (SPEC §20-21).
- **Step success ≠ task success; `finished=true` ≠ SUCCESS.** SUCCESS requires
  independent goal-predicate proof (SPEC §27-29, §56.8-10).
- **Smallest capable model first**; strong models are critics/escalation, not
  default workers (SPEC §11, §15, §56.11-12).
- **Bounded execution:** bounded retries/escalation/recovery; stuck detection
  recognizes semantic-equivalent repeats (SPEC §14, §30, §31).
- **Everything is a replaceable Lego brick behind typed contracts** (SPEC §3):
  engines (llama.cpp #1, Ollama #2, Mock), orchestrator, tools, memory, verifiers.
- **Local inference only** for v0.2 core — no cloud LLM APIs (SPEC §9-10, §54).
- **PostgreSQL is the durable source of truth for task state** (SPEC §34);
  LangGraph checkpoints assist orchestration but are not the truth (SPEC §47).
- **Learning/eval never touches the authority path** (SPEC §33, §52C).
- **No false completion** in our own work either: claim IMPLEMENTED/TESTED/
  VERIFIED only with evidence (SPEC §51).

## Local models

`models/` (repo root, git-ignored) is where the owner places model files. The
directory-scanning provider registers whatever it finds; llama.cpp is\nthe primary serving engine (serves models/ files directly via llama-server), Ollama the compatibility backend. Test with real models
only when the owner says a model was added (hardware-marked tests).

## Commands

```bash
uv sync                                              # install deps
pytest -m "not hardware and not integration"         # unit/contract/security tests
ruff check . && ruff format --check .                # lint
mypy src/                                            # types
docker compose -f docker-compose.dev.yml up -d       # PostgreSQL (integration tests)
```

## Layout

`src/nomadicos/<brick>/` — kernel · contracts · inference · registry · router ·
action_ir · authority · tools · executor · orchestration · verification ·
agents · memory · evaluation · persistence · audit · cli · api. Interfaces
before implementations; fake adapter for every hardware-dependent brick.

## Rebuild phases

SPEC §53: 1 kernel/contracts → 2 inference → 3 registry/router/benchmarks →
4 action IR → 5 owner authority → 6 executor/tools → 7 LangGraph →
8 goal verification → 9 coding worker → 10 worker/critic → 11 memory/object
graph → 12 desktop control → 13 persistence/restart → 14 evaluation/routing.
Follow §52: only the current phase's scope; tests + mypy + ruff before moving on.

## Definition of Done

SPEC §57 checklist — all items must be evidence-backed before claiming done.
