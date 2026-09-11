# NomadicOS — Final Audit Execution Report (LIVE, in progress)

**Companion to (never edited):** `NomadicOS_Qualification_Suite.md`, `docs/NomadicOS_Qualification_Report.md`.
This is the running evidence log for `NOMADICOS_FINAL_AUDIT_EXECUTION_AND_REBUILD_PLAN.md`.
Every claim carries executed evidence and a CONFIRMED / INFERRED / UNKNOWN / MISSING label.

**Status:** STEP 1 complete; confirmed reliability + reproducibility defects fixed with tests.
Steps 2–16 not yet done. **The system is NOT qualified.** Do not read past lines as "release ready".

---

## 1. Current Reality (STEP 1 — forensic verification)

### 1.1 Repository identity (measured, not assumed)

```text
root:            C:\Users\dhruv\OneDrive\Desktop\NomadicOS
git (before):    a873fb9  branch=master  dirty=1 (the plan file, untracked)
git (after):     80689fc
OS:              Windows 11 Home
project runtime: Python 3.12 (.venv) [mypy cfg targets 3.12]  NOTE PATH python is 3.14
node/npm/docker: v24.11.1 / 11.6.2 / 29.6.1
PostgreSQL:      service postgresql-x64-17 = Running (native, ADR-0031)
Ollama:          reachable, 8 models
```

### 1.2 Package inventory

103 tracked `.py` under `src/nomadicos` **after** fix (was 96). Subsystems:
agent, api, audit, backup, benchmark, computer, constitution, core, evaluation,
experience, extensions, learning, **models**, network, postgres, security, tools,
ui, vector, vision. (CLI entry: `cli.py`, `cli_interactive.py`.)

### 1.3 Executed runtime call graph (CASE trace, deterministic fakes)

```text
CLI/API run_goal/goal
  → core/runtime.Runtime  (PG connect, tools, gate wiring)
  → agent/runtime.execute_task
      → _is_conversational  (regex) ──chat?→ _chat_reply (4B)
      └ ambiguous → _classify_intent (model)   [was fail-OPEN → FIXED]
  → selector_agent.select(goal)  (task family → model choice)
  → handler_agent.ensure_model
  → LOOP: _propose(model) → _extract_json → deterministic routing:
        tool present → security/gate.authorize → tools/gateway.execute
                        (unknown tool → PermissionDenied, fail-closed)
        reply only      → chat answer
        malformed       → retryable step failure (no longer "finished")
  → verify (evidence) → audit → report → learn(+) → escalate/retry(+) → PG persist
```

Evidence: `trace_path.py` executed; outputs in §2 (before/after).

---

## 2. Highest-impact CONFIRMED failures (found by executing, not reading diagrams)

### F-REPRO — `src/nomadicos/models/` was UNTRACKED — **CONFIRMED, FIXED**
- Evidence: `git check-ignore` → `.gitignore:20 models/` matched the source package.
- Impact: fresh clone cannot `import nomadicos.models` → **does not run**; "96 modules,
  local model runtime verified" was **environment-local**, not reproducible. This single
  finding validates the audit premise about unverified prior claims.
- Fix: root-scope to `/models/` (+ `node_modules/`); tracked 7 model files. src 96→103.
- Regression: `git ls-files src/nomadicos/models` now returns 7 (verified).

### F-BUDGET — zero/expired duration budget never trips — **CONFIRMED, FIXED**
- Evidence (pre-fix): executed trace `CASE4 … DID NOT RAISE`.
- Root cause: `check_duration` used `elapsed > max`; Windows `monotonic()` ~15.6 ms
  granularity → new-tracker `elapsed==0.0`, `0.0 > 0.0` False. I10 enforcement hole.
- Fix: `>=` (at-or-past deadline is exhausted). Test `test_duration_budget` now green.

### F-CLASSIFIER — intent classifier fail-OPEN → phantom SUCCESS — **CONFIRMED, FIXED**
- Evidence (pre-fix): `CASE1/CASE2 status=SUCCESS completed=[] reply='{"finished": true}' / 'ok'`.
  An imperative/nonsense instruction never reached the executor and leaked raw model text as a
  "reply", reported as success — a direct §6 violation.
- Root cause: `_classify_intent` returned `text != "task"` — ANY non-"task" model output
  (empty, punctuation, injected JSON, confusion) routed to chat. Docstring already promised the
  opposite — code contradicted its invariant.
- Fix: fail-closed — only a positive `chat` answer routes to chat; everything else enters the
  enforced loop where the **gate, not the model**, is authority. Post-fix: `CASE1/CASE2 status=FAILED`.
- Bypass proof: unknown tool via real gateway → `PermissionDenied` fail-closed (gate is sound).
- Regression: `tests/unit/agent/test_intent_routing.py` (fail-closed table), integration tests
  re-asserted to the truthful contract (`test_report_truthfulness_on_failure`,
  `test_invalid_model_proposal_stops_gracefully` — the old `SUCCESS` expectations themselves
  codified the bug).

### F-GENERATED — generated tools could never return a result — **CONFIRMED, FIXED**
- Evidence: `mypy` `"bool" not callable` at `tools/generated.py:134`; code called
  `ToolResult.success(...)` — an API that does not exist (`success` is a `bool` field).
- Impact: the "self-implementing toolbox" path (plan §23) was broken at build time — unrunnable.
- Fix: construct `ToolResult(success=True, data=…)` per the real pydantic contract.
- Regression: `tests/unit/tools/test_generated_contract.py` — a real script executes; valid +
  invalid-JSON cases asserted.

### Structure gaps confirmed but NOT yet fixed (steps 2–4)
- Task state machine exists (`core/lifecycle.TaskState`, transitions) but is **dead code** —
  `git grep` finds no `.transition()`/`TaskState` use outside `lifecycle.py`. The loop mutates a
  raw `TaskStatus` local ⇒ no enforced transitions, **two sources of truth** (plan §9/§31). **MISSING** usage.
- No **canonical typed IR**: model JSON is parsed by hand; no schema_version/enum/unknown-field
  validation feeding a deterministic policy decision (plan §6). **MISSING.**

---

## 3. Pre-existing issues noted, not yet in scope
- `mypy src/nomadicos/` now **fully clean** (100 files) via optional-dep overrides.
- The `portfolio/` static site and its own `docs/FUTURE_PLANS.md` are separate from this core.

## 4. Verification commands + results (reproducible)
```text
uv run pytest tests                → 298 passed, 4 skipped, 0 failed   (was 292/2 fail at STEP 1 start)
uv run --extra dev mypy src/nomadicos → no issues in 100 source files
uv run --extra dev ruff check src tests → All checks passed
executed trace (trace_path.py)    → before/after in §2
```
Note: full-suite run needs PostgreSQL running (skipped=4 are hardware-marked per ADR-0024).

## 5. Honest qualification position right now
Boot/Security foundations look solid and 2 security-relevant defects fixed, but the audit
release bar (**≥99, Boot=100%, Security=100%**) is far from met: no computer control (MISSING),
no coding E2E (MISSING), no self-healing/stress/security-attack harness (MISSING), and the
deterministic core (IR/state) is not wired. **RELEASABLE: NO.** Next: STEP 2 canonical IR.
