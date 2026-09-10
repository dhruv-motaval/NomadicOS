# ADR-0030: Multi-Agent Orchestration

- **Status:** Accepted
- **Date:** 2026-09-07
- **Builds on:** ADR-0027 (phases), ADR-0006 (residency), BP §364 (multi-agent), §244-249 (scheduler)
- **Owner decision:** every task splits into different working agents; work runs in parallel for speed.

## Decision

1. **Orchestrator decomposes** each goal into a subtask DAG via a planner model
   (bounded: ≤ 8 subtasks; dependencies may only reference *earlier* subtask ids —
   the plan is acyclic by construction; invalid plans degrade to single-agent mode).
2. **Wave execution:** subtasks whose dependencies are satisfied run concurrently as
   independent **worker agents**; a step following a completed group consumes all
   group outputs (BP §185 per agent, DAG per plan).
3. **Roles:** `planner` (decompose), `worker` (execute one subtask), `synthesizer`
   (merge results). Same `AgentRuntime` loop, different identity + directive + tools.
4. **Per-agent attribution:** every agent carries an `agent_id` propagated into
   Security Gate decisions, budgets, and audit records (BP §364).
5. **Security Gate above every agent:** workers never call each other directly;
   all tool execution still passes the same gate (I5, BP §375 — no multi-agent
   shortcut around mediation).
6. **Residency under concurrency (ADR-0006 Option A):** agents queue on model
   residency; Ollama serves concurrent requests per model. VRAM multi-slot is
   future work (BP §153).
7. **Failure semantics:** a failed subtask cascades — dependents are marked
   failed/skipped; the final report stays truthful (PARTIAL/FAILED, BP §181).

## Per-subtask model selection

Each subtask names a task family; the ModelSelector picks that subtask's model
independently (coder → qwen3-coder, research → gemma/llama, …). This is the
speed payoff: parallel workers can run on different models concurrently when
resources allow.

## Consequences

- Faster tasks via parallel siblings; sequential chains remain ordered.
- Shared context = scoped MemoryEngine (BP §376-420) + plan outputs with provenance.
- Bounded: max subtasks, per-subtask budgets, total budget (I10).
- Any planner failure ⇒ graceful degradation to the single-agent loop.
