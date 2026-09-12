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

---

# STEP 2 — Canonical Task/Action IR (2026-09-12)

## STATUS: COMPLETE (production runtime uses the IR; raw model->executor path removed)

## Files inspected (map)
- `agent/runtime.py` - the loop (_propose/_extract_json/raw dict .get x13/_mediated_execute/status tail) = the bypass.
- `tools/gateway.py` - deterministic risk from registry (_risk_of), unknown->PermissionDenied. Policy already authoritative; executor took raw strings.
- `security/gate.py` - ALLOW/ASK/DENY/BLOCK decision layer (untouched; consumes tool+risk+identity).
- `core/lifecycle.py` - TaskStatus/TaskState (state machine wiring = STEP 4).
- `security/permissions.py` - SubjectIdentity incl. step_id; grant model (user-only).
- `agent/skills.py` + `agent/orchestrator.py` - competing parse surfaces (removed / verified-none).
- `postgres/*`, `vector/exact.py`, `tools/generated.py`, `evaluation/*`, `api/app.py`, `models/*`, stores - persistence/result JSON, not model-action paths.
- `tests/*`, `demo.py` - replay scripts reviewed for identity-field/queue drift.

## Files changed
- NEW `core/task_ir.py`: ActionClaim (untrusted->validated), TaskAction (bound trusted IR), extract_json_object parser, AUTHORITY_FIELDS denylist, Final schema_version, deterministic to_json. No executable behavior in models.
- `tools/gateway.py`: +action_descriptor(tool,args)->(risk,capabilities) = single deterministic system metadata source for IR binding.
- `agent/runtime.py`: _propose returns ActionClaim; loop consumes typed claim; every step binds a TaskAction carrying task/step/attempt/risk/capabilities before policy/executor; _mediated_execute hard-rejects non-IR objects; cross-attempt failure evidence (all_failures) + specific INVALID reason kept; _audit_task now stamps step_id; dead `_extract_json` removed.
- `agent/skills.py`: dead proposal-replay removed (save(...,proposal=)/find_proposal) - raw model dicts can no longer be a second route to execution.
- `.gitignore`-era BOM artifacts stripped from 13 files (editor hygiene discovered mid-step; behavior-preserving).
- Tests: NEW `tests/unit/core/test_task_ir.py` (matrix 1-10,12,11IR,15IR), NEW `tests/integration/test_task_ir_e2e.py` (4,11,12,13,14,15 real loop+audit+file on disk), stubs updated (`test_machine_profile.py`, `test_skills.py`).

## Post-change execution path (live evidence)
model text -> ActionClaim.from_model_text (INVALID on malformed/authority/extras/oversize; {} NEVER finish) -> loop:
TOOL_CALL -> gateway.action_descriptor (unknown -> stop BEFORE executor; audit shows no ghost ALLOW/ASK) ->
TaskAction.bind(claim,task_id,step_id=attempt-N-step-N,attempt,risk,system-caps) ->
mediated: gateway.execute -> tool schema/path validation -> SecurityGate.authorize(tool,risk from IR+registry,step id) ->
tool.run (workspace-confined) -> verify -> audit step-scoped -> persist. REPLY/FINISH branches never reach executor.
Live Ollama case F: real gemma/qwen run wrote the file, status SUCCESS verified=True, experience+memory persisted.

## Bypass analysis
- policy: only IR tool names with registry risk. Unknown actions blocked at descriptor; never authorized (test D/E + audit assertion).
- executor: _mediated_execute raises ValidationError on non-TaskAction (test 11).
- tool gateway: reachable only THROUGH _mediated_execute (loop) or direct legitimate tool tests; no raw model path.
- task-state mutation: SUCCESS only from chat reply or post-execution completed evidence; never from raw JSON (malformed test C + {} rule + integration tests).
- identity: task/step ids injected by system (IGNORED_IDENTITY_FIELDS); model cannot forge (unit tested).

## TEST EVIDENCE (this session)
- uv run python -m pytest tests -p no:cacheprovider -> 336 passed, 4 skipped (baseline before step2: 298; +38 new/updated, 0 regressions)
- uv run --extra dev mypy src/nomadicos -> Success (101 source files)
- uv run --extra dev ruff check src tests -> All checks passed
- trace_ir.py: A chat=SUCCESS (no executor line), B tool SUCCESS file on disk step attempt-1-step-1, C FAILED unparseable (never finish), D FAILED unknown-tool (policy never saw), E FAILED path escape refused, F live-model SUCCESS incl. memory+experience persistence; audit STEP_DONE step_ids verified.

## Regressions
- None functional. Two test-stub attributes updated; existing integration semantics preserved (challenge/escalation/repeat-guard intact).

## Remaining issues (confirmed, deferred to later steps)
- FilesystemTool accepts nested relative paths -> model-supplied data/task-workspaces/ir-live.txt landed one level deep; sandbox containment held but path UX oddity = STEP 3/6 cleanup item.
- TaskState enum still unused by loop -> STEP 4 (state machine wiring).
- Capability vocabulary is tool-derived (filesystem.read etc.) -> formal capability model in STEP 3.

## CLAIM CONFIDENCE
- Execution path uses canonical IR: CONFIRMED (unit + integration + live F).
- Bypass closed at four surfaces: CONFIRMED by tests (not inspection) .
- Policy authority unchanged (gate still decides): CONFIRMED.
- "Nothing bypassed" beyond traced paths: INFERRED (static grep + tests cover model-origin paths; STEP 3 attack suite will probe further).

---

# STEP 4 — Unified Task State (authoritative lifecycle) 2026-09-12

## Implementation
- core/lifecycle.py: CREATED/AUTHORIZED/BLOCKED added; FAILED/PARTIALLY -> RECOVERING
  retry chain; RUNNING/WAITING/VERIFYING -> BLOCKED removed except RUNNING;
  INTERRUPT_STATUSES + TERMINAL_STATUSES define recovery semantics.
- repositories: create -> status='CREATED'; advance_status(expected,to) CAS with
  StateConflict; reconcile_interrupted() -> FAILED for interrupted, BLOCKED survives.
- migration 004_tasks_lifecycle_status.sql (CHECK + default).
- agent/runtime execute_task: single TaskState owner. _advance = validate ->
  persist CAS -> flip. Mid-flight persistence = HARD (StatePersistenceError ->
  honest FAILED report, no phantom state). Terminal/ensure = soft best-effort.
  Every attempt/retry/verify/cancel/blocked path goes through it; stop callback
  cancels + persists CANCELLED; blocked ask persists BLOCKED.
- core/runtime: state_sink + stop_requested wiring; lazy startup reconcile;
  orchestration row walked through the same legal transitions.

## Verification (this turn, live)
- Unit lifecycle + PG persistence + duplicate-completion: all green.
- Live trace A (real Ollama write goal): report SUCCESS == db SUCCESS,
  step states attempt-1-step-1 ALLOW->EXECUTED->STEP_DONE.
- Live trace B (System32 write attempted by real model): report FAILED ==
  db FAILED across TWO attempts (attempt-2-step-N audit proves retry state).
- Full: pytest 348 passed 4 skipped (baseline 336); mypy 101 files clean;
  ruff clean. Qualification docs untouched (git-scoped check).
- Remaining: step-level per-row state of steps themselves stays in audit
  STEP_DONE events (tasks table remains the task authority); api GoalRegistry
  entry.status + orchestrator final_status strings are DERIVED views (justified),
  not stores.

---

# STEP 3 — Deterministic policy / capability enforcement 2026-09-12

## New: security/capability_registry.py
Formal registry (ids only the runtime can enforce today): filesystem.read/
write/list/delete, terminal.execute, network.fetch,
generated_tool.execute. Each cap: id, operation, resource_kind, risk class,
requires_user_authorization, network/sandbox/audit flags. is_managed() keeps
unknown FILESYSTEM actions denied; owner-registered tools without explicit
enumeration get deterministic generic contract (fake.echo.execute style,
verified via EchoTool). resolve(tool,args) = single capability-resolution
site; unknown => PermissionDenied(CAPABILITY_NOT_REGISTERED) BEFORE policy.

## Gate now authoritative-deterministic
- authorize(tool, risk, identity, arguments, classification, capability, resource)
  -> SecurityDecision(+capability, resource, reason_code, policy_version, audit_id).
- capability.requires_user_authorization: filesystem.delete AND
  generated_tool.execute ALWAYS need an explicit owner grant; a granted-once
  one-shot grant is consumed after ALLOW so repeats re-ask (step-9 rule:
  generated tools never trusted for having worked before).
- risk comes from the REGISTRY (filesystem.delete=CRITICAL was previously
  hidden behind spec-level "medium") — never model-supplied fields.
- Network: localhost/loopback/private/link-local/metadata/bad-scheme/no-host
  -> BLOCK reason_code NON_PUBLIC_DESTINATION / INVALID_SCHEME etc. Deterministic
  (no DNS); public hostnames allowed; .local/.internal blocked.
- policy_version via PolicyEngine.document_version.
- Every refusal (also registry-stage denials via gateway.audit_denial)
  appends TOOL_DECISION audit with reason_code/capability/resource; audit_id
  equals decision.audit_id (event_id passed through). No arg dumps logged.

## ToolGateway
- action_descriptor + resolve_capability route through registry; execute
  pipeline order: registry resolve -> schema -> budget -> policy -> exec ->
  audit(+resource label). describe_resource: path/command/url/script only
  (truncated 240 chars I12).
- filesystem.safe_resolve: deterministic workspace-prefix echo normalization
  (C:\Windows\System32\... and data/task-workspaces/x stay confined;
  task-workspaces/file.txt -> file.txt; traversal and external escapes
  rejected pre-policy).

## Tests: tests/security/test_capability_authority.py (all 12 plan items, 18
functions incl. parameterized fake-authority fields, escalation by retry /
stronger model / memory / planner / tool output / generated tools,
path/network/determinism/audit checks).
Live trace (temp/live_s3.py): A SUCCESS; B escape refused; C system.root
registry-denied + audited; D unknown action denied; E allowed/risk fields
rejected pre-policy; F hostile text = data only; G identical retry outcomes;
H ASK => executor never reached (runtime status BLOCKED).

## Full run
uv run pytest tests -> 366 passed, 4 skipped (from 348 pre-step-3, +18).
mypy src/nomadicos: Success 101+2 files. ruff: clean. STEP-2 IR and STEP-4
lifecycle regression suites pass unchanged.

## NOT at 100% yet / notes
- SSRF via DNS-resolving-to-private hostnames: UNKNOWN residual (no resolver
  check by design; note for later).
- symlink/junction escape inside workspace: path.resolve() applied BEFORE
  containment; test only proves textual traversal; live-symlink case MISSING.
- git.write/process.spawn/desktop.* capabilities intentionally absent
  (runtime cannot enforce yet).
