# Current phase — v0.2 REBUILD IN PROGRESS (started 2026-09-15)

The v0.1 architecture (Canonical Blueprint, 14 phases, PostgreSQL-first,
always-confirm Security Gate) was **wiped on 2026-09-15** by owner directive
and is replaced by the rebuild in `NOMADICOS_REBUILD_MASTER_SPEC_FINAL.md`
(SPEC). The old git history remains for recovery.

Work strictly through SPEC §53 phases; report evidence per SPEC §51:

- Phase 1–2: kernel/contracts + inference bricks
- Phase 3+: registry/router, action IR, owner authority (persistent
  FULL_PC_AUTONOMY replaces v0.1 always-confirm model), executor/tools
  (real terminal), LangGraph graph, goal verification, coding worker,
  critic loop, memory/object graph, desktop foundation, PostgreSQL
  persistence + restart recovery, evaluation/benchmarks.

`models/` at repo root holds the owner's local model files — scan it for
real-model testing only when the owner says a model was added. Engine\npriority: llama.cpp (#1), Ollama (#2), Mock (tests).
