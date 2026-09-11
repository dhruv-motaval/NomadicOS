# NomadicOS — Final Audit Execution, Architecture Correction & Validation Plan

**Purpose:** This is a NEW document. Do not edit `docs/NomadicOS_Qualification_Report.md` or any prior audit document. Use this file as the authoritative implementation/review instruction for the next code-generation pass.

**Evidence basis:** the supplied `NOMADICOS_EXPLAINED.md`, plus the saved `NomadicOS_Qualification_Suite.md` where available. The supplied explanation reports a prior score of **61/100**, a recalculated **~64/100** after semantic-memory work, and five major remaining gaps: computer control, coding-agent end-to-end validation, self-healing probes, stress/soak testing, and later measured model improvement. The qualification suite sets the release bar at **99/100**, with **Boot = 100%** and **Security = 100%**. fileciteturn0file0L74-L106 fileciteturn4file0L15-L25

---

# 1. Non-Negotiable Rule

Do not optimize for making the architecture look complete.

Optimize for making the system **actually work and produce evidence**.

The previous model output is not accepted merely because it says:

- fixed
- verified
- secure
- semantic
- autonomous
- self-healing
- production-ready
- end-to-end
- 99/100

Every such claim must have:

1. exact file/module/function evidence,
2. an executed test,
3. expected result,
4. actual result,
5. repeatability,
6. regression coverage where appropriate.

The qualification suite explicitly requires every failure to have a symptom, root cause, evidence, fix, and regression test. fileciteturn4file0L15-L25

---

# 2. What You Are Receiving From the Previous Pass

The supplied system-status document currently claims:

```text
NomadicOS
├── local CLI
├── local API
├── intent router
├── selector/model routing
├── model handler
├── proposal/execution loop
├── security gate
├── budgets
├── terminal
├── filesystem
├── web.fetch
├── machine profile
├── semantic memory using local nomic-embed-text
├── conversation memory
├── skill learning
├── generated tools
├── escalation
└── append-only audit
```

It also claims the current execution loop is:

```text
input
  ↓
router
  ↓
selector
  ↓
model handler
  ↓
propose
  ↓
security gate
  ↓
tool
  ↓
verify
  ↓
report
  ↓
learn / retry
```

and that semantic memory, duplicate-execution prevention, terminal argv handling, malformed proposal handling, conversation continuity, and several routing/tooling defects were already fixed. fileciteturn0file0L15-L45 fileciteturn0file0L93-L103

**Treat all of those as claims to verify against the repository and runtime before relying on them.**

---

# 3. First Mission: Forensic Verification

Before writing substantial new code, perform a repository-wide audit.

## 3.1 Identify the actual repository

Report:

```text
repository root:
git revision:
branch:
dirty tree:
OS:
Python/runtime:
Node/runtime:
Docker:
PostgreSQL:
Ollama:
other dependencies:
```

## 3.2 Produce the actual tree

Ignore generated/cache/vendor directories unless they affect runtime.

For every important folder, state:

```text
path
purpose
entry points
imports from/to
runtime relevance
```

## 3.3 Find the real runtime path

Trace an actual request from:

```text
CLI/API
→ input parsing
→ intent classification
→ planning/proposal
→ model selection
→ security authorization
→ tool execution
→ verification
→ state persistence
→ reporting
→ learning/retry
```

Do not infer the path from diagrams.

Use the code and an executed request.

## 3.4 Build a runtime call graph

For one successful request, produce:

```text
ENTRY
  → FILE:FUNCTION
  → FILE:FUNCTION
  → FILE:FUNCTION
  → ...
RESULT
```

For every step record the relevant request/response structure.

---

# 4. Stop the Common Code-Generator Failure Mode

The generator must NOT respond with generic prose such as:

> "The architecture is good but could be improved."

Instead, every architecture claim must use this structure:

```text
CLAIM:
EVIDENCE:
FILE:
FUNCTION/CLASS:
RUNTIME TEST:
EXPECTED:
ACTUAL:
CONFIDENCE:
```

For every proposed change:

```text
PROBLEM:
ROOT CAUSE:
CURRENT CODE:
DESIRED INVARIANT:
IMPLEMENTATION:
TEST:
REGRESSION TEST:
```

If something cannot be established:

```text
UNKNOWN — NOT VERIFIED
```

Do not fabricate an answer.

---

# 5. Architecture Target

NomadicOS should be reorganized around a small deterministic core.

The target architecture is:

```text
                    ┌───────────────────┐
                    │       INPUT       │
                    │   CLI / Local API │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │ INTENT / PLANNER  │
                    │       LLM         │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │   CANONICAL IR    │
                    │ typed task/action │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │   POLICY CORE     │
                    │ deterministic     │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │     SCHEDULER     │
                    │ task lifecycle    │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │     EXECUTOR      │
                    │ deterministic     │
                    └────┬────────┬─────┘
                         ↓        ↓
                   ┌────────┐ ┌──────────┐
                   │ Sandbox│ │ Tool     │
                   │/OS     │ │ Gateway  │
                   └────────┘ └──────────┘
                         │        │
                         └────┬───┘
                              ↓
                    ┌───────────────────┐
                    │       RESULT      │
                    └─────────┬─────────┘
                              ↓
                    ┌───────────────────┐
                    │ VERIFY / AUDIT    │
                    └───────────────────┘


SECONDARY SYSTEMS:

Memory
Learning
Model routing
Benchmarks
Experience DB
Chat history
Skills
Evaluation
```

## Critical rule

> The LLM may propose intent or a typed action. It must never be the final authority that decides whether an operation is executable.

The final decision must be made by deterministic policy/capability enforcement.

---

# 6. Canonical Task/Action Contract

Create or identify one canonical internal representation.

At minimum:

```json
{
  "schema_version": "1",
  "task_id": "uuid",
  "goal": "human-readable goal",
  "steps": [
    {
      "step_id": "uuid",
      "action": "filesystem.read",
      "arguments": {},
      "capabilities": [],
      "risk": "low"
    }
  ]
}
```

Requirements:

- schema validation,
- strict types,
- enum validation,
- unknown-field handling,
- schema versioning,
- stable serialization,
- deterministic authorization input.

Malformed model output must never be interpreted as:

```text
finished
success
approved
safe
```

Test:

```text
missing field
wrong type
unknown action
unknown capability
extra privilege field
invalid path
invalid enum
null
oversized arguments
```

---

# 7. Deterministic Policy Core

Audit and harden `security/gate.py` or its replacement.

The policy engine must answer:

```text
Is this exact operation allowed?
For this actor?
Against this resource?
For this task?
Under this policy?
At this risk level?
```

The policy decision should be reproducible from:

```text
actor
task
capability
operation
resource
policy
security labels
```

Do not derive authorization from natural-language model output.

Security must remain enforceable even if the model is malicious, confused, or prompt-injected.

---

# 8. Capability Model

Implement or formalize capabilities such as:

```text
filesystem.read
filesystem.write
filesystem.delete
terminal.execute
process.spawn
process.kill
network.fetch
browser.navigate
browser.download
desktop.observe
desktop.mouse
desktop.keyboard
git.read
git.write
python.execute
generated_tool.execute
memory.read
memory.write
```

Each operation must declare:

```text
capability
risk class
required approval
allowed resource scope
audit requirements
sandbox requirements
```

No implicit capability grants.

---

# 9. Task State Machine

Create a real state machine.

Minimum:

```text
CREATED
PLANNED
AUTHORIZED
RUNNING
WAITING
COMPLETED
FAILED
CANCELLED
BLOCKED
RECOVERY
```

Define legal transitions.

Example:

```text
CREATED → PLANNED
PLANNED → AUTHORIZED
AUTHORIZED → RUNNING
RUNNING → COMPLETED
RUNNING → FAILED
FAILED → RECOVERY
RECOVERY → RUNNING
RUNNING → CANCELLED
```

Requirements:

- task IDs,
- step IDs,
- transition validation,
- persisted state,
- restart recovery,
- no double completion,
- no execution after cancellation,
- explicit partial-completion state.

---

# 10. Executor Simplification

The executor should not own everything.

Separate these responsibilities where currently coupled:

```text
Planner
Policy
Scheduler
Executor
Verifier
Persistence
Audit
Memory
Learning
```

The executor should primarily do:

```text
authorized action
→ execute
→ collect result
→ return structured result
```

It should not decide policy.

It should not rewrite its own permissions.

It should not silently trigger arbitrary retries.

It should not treat model text as authoritative state.

---

# 11. Memory: Keep the Semantic Fix, But Make It Production-Grade

The supplied explanation says semantic recall is now implemented through local `nomic-embed-text`, with IDF fallback when Ollama is unavailable. fileciteturn0file0L133-L141

Verify this claim.

Then improve memory around these boundaries:

```text
Conversation
Working memory
Long-term memory
Task state
Machine profile
Skill memory
Experience
Audit log
```

They must not be treated as interchangeable.

Every memory record needs provenance.

Recommended fields:

```text
memory_id
scope
project_id
task_id
source
trust_level
created_at
updated_at
content
embedding
version
sensitivity
```

Critical security rule:

> Memory is data. Memory is not authority.

A malicious memory entry must not become a policy instruction.

---

# 12. Chat/Context Injection

Audit how context is built.

Separate:

```text
system authority
policy
current task
trusted machine facts
memory
conversation
tool output
web content
user content
```

Use explicit trust boundaries.

Do not concatenate all of these into one undifferentiated prompt.

Test prompt injection from:

```text
conversation
memory
file content
tool output
web page
generated skill
generated script metadata
```

Expected result:

```text
untrusted content can inform planning
but cannot rewrite authority/policy
```

---

# 13. Model Router

The current document reports:

```text
general → gemma-3-4b
automation/coding → qwen3-coder:30b
reasoning → nomad-oss 20B / qwen3:14b
```

and stronger-model escalation on retry. fileciteturn0file0L62-L78

Verify actual runtime behavior.

Do not rely on model names as permanent truth.

Create a model registry:

```text
model_id
provider
context_limit
tool_support
structured_output
thinking_support
vision_support
estimated VRAM
measured latency
task-family score
availability
```

Routing should eventually be based on measured task performance, not simply model size.

Do not implement fine-tuning before this measurement system exists.

---

# 14. Coding-Agent End-to-End Completion

This is one of the highest-priority missing capabilities.

The current document says coding support is partial and still needs compiler/toolchain availability. fileciteturn0file0L80-L91

Build an actual E2E coding validation.

## Required task

Have NomadicOS create a small project in an isolated workspace.

Example:

```text
simple Markdown editor
- editor
- autosave
- PDF export or placeholder
- search
- word count
- dark mode
```

The exact stack may be chosen based on installed local tooling.

The agent must:

```text
1. inspect environment
2. select/create workspace
3. create project
4. write source files
5. write tests
6. install dependencies through authorized mechanisms
7. run tests
8. detect failures
9. modify code
10. rerun tests
11. produce build
12. inspect build result
13. write README
14. show final evidence
```

The qualification suite requires the coding test to generate README, tests, build, and documentation. fileciteturn4file0L81-L100

## Acceptance

No "I would do X" answer.

The system must actually do it.

---

# 15. Self-Debugging / Self-Healing Separation

Do not mix two different ideas:

```text
self-debugging
```

means diagnosing and repairing a task.

```text
self-healing
```

means detecting system/subsystem failure and recovering service/state.

Both need explicit tests.

---

# 16. Coding Self-Debugging Test

Create disposable test failures:

```text
null reference / equivalent
infinite loop
missing dependency
race condition
resource leak
```

The qualification suite requires:

```text
detect
reproduce
explain
patch
verify
```

for the coding-agent self-debugging scenario. fileciteturn4file0L101-L118

Do not allow the agent to claim success from syntax alone.

Success must be supported by executed tests.

---

# 17. Self-Healing: Phase 7

This is currently reported as unbuilt. fileciteturn0file0L84-L91

Implement controlled probes.

## Probe A — PostgreSQL failure

Procedure:

```text
1. begin safe task
2. interrupt database connectivity
3. observe failure
4. reconnect
5. replay/reconcile safe pending writes
6. verify no duplicate state
```

The qualification suite requires detection, reconnect, and replay behavior. fileciteturn4file0L236-L245

## Probe B — Configuration deletion

```text
1. back up test configuration
2. delete selected config artifact
3. restart subsystem
4. regenerate defaults
5. preserve user settings where defined
6. verify configuration integrity
```

## Probe C — Vector/index corruption

```text
1. corrupt disposable vector/index state
2. detect corruption
3. rebuild
4. verify checksum/integrity
5. verify retrieval still works
```

Every probe must have:

```text
failure injected
failure detected
recovery action
recovered state
data integrity result
audit event
```

---

# 18. Computer Control: Phase 6

The supplied explanation explicitly says keyboard/mouse/screen control is not built. fileciteturn0file0L80-L91

Do not add random automation libraries just to claim completion.

First define an OS abstraction:

```text
DesktopObserver
KeyboardController
MouseController
WindowManager
ApplicationLauncher
ScreenCapture
```

Then provide a tool gateway:

```text
desktop.observe
desktop.click
desktop.type
desktop.keypress
desktop.move
desktop.launch
desktop.focus
desktop.close
```

All must remain policy-gated.

---

# 19. Computer-Control Closed Loop

The intended behavior must be:

```text
OBSERVE
  ↓
PLAN
  ↓
ACT
  ↓
OBSERVE AGAIN
  ↓
VERIFY
  ↓
RECOVER / CONTINUE
```

Never:

```text
LLM blindly clicks coordinate X
```

unless that is the explicitly selected low-level mechanism and the post-action observation verifies the result.

Minimum safe test:

```text
open Notepad
type a known string
save to disposable location
reopen
read/verify content
close
```

Then:

```text
open VS Code
create hello.py
run it
verify output
```

The existing qualification suite explicitly calls for opening VS Code, creating `hello.py`, executing it, and verifying the result. fileciteturn4file0L142-L156

---

# 20. Browser Capability

Separate:

```text
HTTP fetch
```

from:

```text
interactive browser control
```

The current `web.fetch` capability is not equivalent to browser automation.

The qualification suite requires:

```text
search
read
download
upload
fill form
screenshot
extract table
```

for browser validation. fileciteturn4file0L158-L167

Do not claim browser automation is complete when only GET requests work.

---

# 21. Tool Contract

Every tool must return structured JSON.

Minimum:

```json
{
  "ok": true,
  "tool": "filesystem.read",
  "request_id": "uuid",
  "data": {},
  "error": null,
  "observations": [],
  "duration_ms": 123
}
```

Failures must be machine-readable.

Never make the model infer success from prose like:

```text
"command seems to have worked"
```

---

# 22. Retry and Idempotency

The current system reportedly has three retries and an exact-repeat guard. fileciteturn0file0L48-L50 fileciteturn0file0L93-L99

Verify both.

For each side-effecting tool, define:

```text
idempotent?
deduplication key?
safe retry?
rollback possible?
reconciliation method?
```

Test:

```text
execute
crash before result persistence
restart
recover
```

The same side effect must not happen twice unless explicitly permitted.

---

# 23. Generated Tools / Self-Modification

The current document says successful scripts can become permanent tools in `data/scripts/`. fileciteturn0file0L57-L60

This is a major security boundary.

Generated code must not automatically become trusted system code.

Require at minimum:

```text
source provenance
hash
version
creator task ID
permissions
dependency list
validation result
sandbox status
approval state
```

Generated tools should execute under the same or stricter policy than normal tools.

A successful previous task must not automatically grant more privilege.

---

# 24. Learning Isolation

Learning must not silently rewrite production behavior.

Use:

```text
production execution
      ↓
experience record
      ↓
offline evaluation
      ↓
candidate change
      ↓
benchmark
      ↓
regression suite
      ↓
promotion
      ↓
production
```

Do not use:

```text
production failure
  ↓
automatic self-modification
  ↓
new production behavior
```

without a controlled promotion boundary.

---

# 25. Stress Harness: Phase 10

This is currently reported as unbuilt. fileciteturn0file0L80-L91

Create a repeatable stress test.

## Parallel test

Run **100 simultaneous harmless tasks** using a controlled test environment:

```text
chat
memory
filesystem
python
search
git
```

The qualification suite expects no deadlocks and no crashes. fileciteturn4file0L295-L312

Record:

```text
success rate
failure rate
timeouts
deadlocks
duplicate executions
memory contamination
database contention
CPU
RAM
VRAM
open handles
latency percentiles
```

Do not run destructive operations in the stress harness.

---

# 26. 24-Hour Soak

Implement the qualification suite's long-run test only after the shorter stress test is stable.

Every minute:

```text
memory write
chat
tool call
file operation
```

Monitor:

```text
RAM
VRAM
handles
database connections
CPU
disk growth
task queue
orphan processes
```

Expected:

```text
zero unbounded leaks
zero unexplained growth
zero corrupted state
```

The qualification suite defines this as a required soak test. fileciteturn4file0L313-L330

---

# 27. Performance Benchmarking

The current qualification targets are:

| Metric | Target |
|---|---:|
| CLI startup | < 2s |
| First token | < 500ms |
| Memory search | < 100ms |
| File search | < 300ms |
| Tool call | < 200ms |
| Planning | < 2s |

fileciteturn4file0L280-L291

Measure before optimizing.

For model-heavy operations, separate:

```text
framework overhead
model load
model inference
tool execution
database
verification
```

Do not blame the model for framework latency.

---

# 28. Security Validation

The qualification suite explicitly requires attempts against:

```text
dangerous command
registry edits
credential access
protected folders
System32 writes
```

with:

```text
authorization request
audit entry
no silent execution
```

fileciteturn4file0L262-L277

Run these against disposable/test targets where necessary.

Also test:

```text
prompt injection
path traversal
command injection
secret leakage
malicious tool output
malicious memory
malicious generated skill
privilege escalation through retries
```

A security test passes only when the enforcement boundary, not merely the model's refusal, prevents the operation.

---

# 29. Failure Injection Matrix

Build a reusable failure-injection harness.

At minimum:

```text
model unavailable
model timeout
invalid model JSON
Ollama unavailable
database unavailable
database restart
database write failure
memory search failure
embedding failure
tool timeout
tool crash
tool malformed response
filesystem permission error
workspace missing
process killed
config missing
index corrupted
duplicate request
cancelled task
concurrent same-task request
```

For each:

```text
detect?
logged?
recover?
retry?
duplicate side effect?
final task state?
```

---

# 30. End-to-End Qualification Order

Do not work randomly.

Use this order:

```text
STEP 1 — Forensic verification
STEP 2 — Canonical IR + schema validation
STEP 3 — Deterministic policy/capability enforcement
STEP 4 — Task state machine
STEP 5 — Executor separation
STEP 6 — Tool structured-result contract
STEP 7 — Coding-agent E2E
STEP 8 — Computer control
STEP 9 — Self-debugging
STEP 10 — Self-healing
STEP 11 — Security attack suite
STEP 12 — Concurrency tests
STEP 13 — Stress harness
STEP 14 — Performance benchmarks
STEP 15 — Regression suite
STEP 16 — Final qualification
```

Do not spend significant effort on fine-tuning before this is stable.

---

# 31. What Must Be Deleted or Simplified If Found

The code generator must explicitly identify architecture that should be removed, not only added.

Candidates include:

```text
raw reasoning used as execution protocol
duplicate policy checks with conflicting semantics
multiple task-state stores
multiple sources of truth
chat history treated as authority
memory treated as authority
learning directly inside the production control path
executor-owned policy decisions
unbounded recursive agent loops
implicit capability grants
provider-specific behavior leaking above the model adapter
```

Do not preserve bad abstractions merely because they already exist.

---

# 32. What Must NOT Be Added Yet

Do not add these merely for architectural fashion:

```text
Kubernetes
microservices
distributed queues
cloud model providers
vector databases solely because they are popular
MCP as a replacement for the core runtime
more agents for the sake of having more agents
fine-tuning before benchmark data exists
RAG pipelines unrelated to an observed retrieval problem
```

NomadicOS is local-first. Keep the architecture local unless a requirement demonstrably needs external infrastructure.

---

# 33. Required Tests Before Any "Fixed" Claim

For each fix:

```text
unit test
integration test
end-to-end test when behavior crosses subsystem boundaries
negative test
regression test
```

For security-sensitive behavior:

```text
authorized case
unauthorized case
malicious-input case
restart case
concurrent case
```

---

# 34. Required Final Evidence Package

The code generator must produce these files/artifacts:

```text
docs/NOMADICOS_FINAL_AUDIT_EXECUTION_REPORT.md
docs/NOMADICOS_ARCHITECTURE_CURRENT.md
docs/NOMADICOS_ARCHITECTURE_TARGET.md
docs/NOMADICOS_FAILURE_MATRIX.md
docs/NOMADICOS_SECURITY_VALIDATION.md
docs/NOMADICOS_PERFORMANCE_REPORT.md
docs/NOMADICOS_STRESS_REPORT.md
docs/NOMADICOS_E2E_REPORT.md
```

and a test/benchmark directory appropriate to the repository.

Do not overwrite the historical qualification report.

---

# 35. Final Report Structure

The final execution report must contain exactly these sections:

## 1. Current Reality

What exists and was actually verified.

## 2. Architecture

Current architecture vs target architecture.

## 3. Changes Made

Every changed file and why.

## 4. Removed/De-scoped Components

What was deleted or intentionally not implemented.

## 5. Tests Executed

Exact commands.

## 6. Test Results

Pass/fail with evidence.

## 7. Root Causes

Only confirmed or clearly labeled inferred causes.

## 8. Security Findings

Attack and defense results.

## 9. Recovery Findings

Failure-injection and recovery behavior.

## 10. Stress/Performance

Metrics and resource behavior.

## 11. Remaining Gaps

No vague language.

## 12. Qualification Score

Recalculate from actual test evidence.

## 13. Release Decision

Exactly one:

```text
RELEASED
RELEASE BLOCKED
```

---

# 36. Qualification Score Rules

Do not invent points.

The qualification suite expects:

```text
overall ≥ 99/100
Boot = 100%
Security = 100%
```

before release. fileciteturn4file0L15-L25 fileciteturn4file0L366-L381

Use the actual scorecard:

| Category | Weight |
|---|---:|
| Boot | 10 |
| Chat | 15 |
| Memory | 15 |
| Coding | 20 |
| Tool Use | 15 |
| PC Control | 10 |
| Security | 10 |
| Recovery | 5 |
| **TOTAL** | **100** |

Do not award points for "implemented but untested."

An unverified capability scores as unverified, not passed.

---

# 37. Important Conflict to Resolve

The supplied `NOMADICOS_EXPLAINED.md` says the system is local-only and nothing leaves the machine, but it also reports a `web.fetch` capability. fileciteturn0file0L9-L13 fileciteturn0file0L51-L55

Do not call this a contradiction automatically.

Determine the exact policy:

```text
local model only?
local memory only?
web access allowed?
remote web endpoint allowed?
remote AI provider allowed?
offline mode available?
```

The final report must explicitly document the network boundary.

---

# 38. Important Architectural Target: Observe → Plan → Act → Verify

For every real-world automation task:

```text
OBSERVE
  ↓
PLAN
  ↓
POLICY CHECK
  ↓
ACT
  ↓
OBSERVE
  ↓
VERIFY
```

The loop must stop when:

```text
success
failure
blocked
cancelled
budget exhausted
```

No hidden infinite loops.

---

# 39. What "Working Well" Means

NomadicOS is working well only when all of these are true:

```text
1. It interprets the user's goal correctly.
2. It produces a strict structured plan.
3. Policy deterministically authorizes/rejects each action.
4. Execution is isolated and reproducible.
5. Results are structured.
6. Verification uses evidence, not model confidence.
7. State survives restart.
8. Retry does not duplicate side effects.
9. Memory retrieves relevant information without becoming authority.
10. Prompt injection cannot rewrite security boundaries.
11. Tools cannot silently bypass policy.
12. Generated tools do not gain privilege automatically.
13. Failures are diagnosable from logs.
14. System failures recover safely.
15. Multiple tasks do not contaminate one another.
16. Computer actions are observed and verified.
17. Coding workflows actually build/test/repair software.
18. Performance is measured.
19. Stress behavior is measured.
20. The qualification suite passes with evidence.
```

---

# 40. Instructions for the Code Generator When It Gets Stuck

Do not return a vague answer.

Instead:

### A. Inspect

Search the repository for the relevant implementation.

### B. Trace

Follow imports/callers/callees.

### C. Execute

Run the smallest reproduction.

### D. Instrument

Add temporary diagnostics if necessary.

### E. Isolate

Determine which subsystem owns the fault.

### F. Fix

Make the smallest correct architectural change.

### G. Regression test

Encode the failure permanently.

### H. Re-run

Run the affected test set.

### I. Report

Give evidence.

When multiple plausible causes exist:

```text
Hypothesis 1
Hypothesis 2
Hypothesis 3
```

Test them rather than selecting one by intuition.

---

# 41. Deep Questions the Generator Must Ask Itself

Before declaring the system healthy, answer all of these with evidence:

### Control

- What component has final authority to execute?
- Can model text bypass the policy engine?
- Can a tool invoke another tool without policy?
- Can a retry upgrade privileges?

### State

- Where is task state stored?
- Can two stores disagree?
- What survives restart?
- What happens after a crash between side effect and result persistence?

### Memory

- Who may write memory?
- Who may read memory?
- Can memory change behavior?
- Can malicious memory become instructions?
- Is semantic retrieval measurable?

### Models

- What happens when Ollama is down?
- What happens when the selected model cannot follow the schema?
- Can fallback change task semantics?
- How is the best model selected?

### Tools

- Does every tool return structured output?
- Is every side effect auditable?
- Is every retry safe?
- Can a tool execute outside its intended resource scope?

### Desktop

- How is screen state observed?
- How are actions verified?
- How are coordinates/stale UI handled?
- How is the focused application verified?

### Recovery

- Can database failure be recovered?
- Can index corruption be rebuilt?
- Can missing config be restored?
- Can an interrupted task resume safely?

### Concurrency

- Can tasks share memory accidentally?
- Can task A use task B's workspace?
- Can duplicate requests run simultaneously?
- Are DB transactions sufficient?

### Security

- Can secrets reach the prompt?
- Can retrieved content rewrite authority?
- Can generated code gain privilege?
- Can web content become an instruction?
- Can a model disable safeguards?

### Observability

- Can one task be replayed from logs?
- Can every policy decision be explained?
- Can a failed tool call be traced to its task/model/step?
- Can performance regressions be detected?

If any answer is unknown, mark it unknown and add a test or instrumentation task.

---

# 42. Final Principle

The project does not need more architectural decoration.

It needs:

```text
deterministic core
+
strict contracts
+
explicit state
+
hard capability boundaries
+
real computer control
+
real coding E2E
+
real recovery
+
real stress tests
+
measured evidence
```

The supplied status document reports that semantic memory is now fixed and that the system has additional defect fixes. Those should be preserved only after repository/runtime verification. fileciteturn0file0L80-L106

The remaining work should not be treated as a checklist of features. It is a qualification program.

**Do not declare NomadicOS complete until the actual implementation satisfies the qualification suite with reproducible evidence and reaches the required release threshold.**
