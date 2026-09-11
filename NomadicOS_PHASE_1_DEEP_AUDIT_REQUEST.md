# NomadicOS — Architecture & Codebase Deep-Dive Request
## Phase 1: Evidence Collection for an External Code Generator

**Purpose:**  
This document is a forensic information request. Do not redesign NomadicOS yet. Do not give generic advice. First inspect the actual repository, execute the system, trace the runtime, test the boundaries, and document what really exists.

The next step is to return a completed copy of this document (or a companion Markdown response) with concrete evidence. That response will be reviewed separately to produce the final architecture-change plan.

---

# 0. Non-Negotiable Rules

## 0.1 Evidence over assumptions

For every important claim, distinguish:

- `CONFIRMED` — directly observed in code, configuration, logs, tests, or an executed command.
- `PARTIALLY_CONFIRMED` — some evidence exists, but coverage is incomplete.
- `INFERRED` — a reasoned conclusion from evidence.
- `UNKNOWN` — cannot be determined from the repository/runtime.
- `MISSING` — the expected capability does not exist or could not be found.

Do **not** fill gaps with assumptions.

## 0.2 Inspect before explaining

Before proposing changes, inspect:

- repository tree
- entry points
- package/module boundaries
- configuration
- dependencies
- database/schema/migrations
- prompts
- model/provider adapters
- tool system
- security/policy code
- memory
- task/state management
- execution/sandbox code
- tests
- scripts
- logs
- Docker/containers/services
- CI/CD
- documentation that claims behavior

## 0.3 Run the system

Do not merely read code.

Run as much of the real system as safely possible.

Capture:

- exact commands
- exit codes
- stdout/stderr
- startup sequence
- health checks
- API/CLI behavior
- model calls
- tool calls
- database behavior
- failures
- timeouts
- retries
- unexpected state changes

If something cannot be run, explain exactly why.

## 0.4 Do not "fix" code during this phase

Do not modify the repository unless a temporary test artifact is absolutely required.

If you do modify anything temporarily:

1. state exactly what changed,
2. isolate it,
3. revert it,
4. confirm the original tree is restored.

## 0.5 Attack your own conclusions

For each major conclusion, try to disprove it.

Example:

> "Security blocks unauthorized tools."

Then run a test that attempts to reach the same tool through every alternate path you can identify.

The objective is not to make NomadicOS look good. The objective is to discover why it currently fails.

---

# 1. Repository Identity

Fill this section first.

### Repository root

```text
<path>
```

### Commit / revision

```text
<commit hash or equivalent>
```

### Branch

```text
<branch>
```

### Dirty working tree?

```text
YES / NO
```

If yes, summarize uncommitted changes:

```text
...
```

### Operating system

```text
...
```

### Runtime versions

```text
Python:
Node:
Docker:
PostgreSQL:
Other:
```

### How NomadicOS is started

```bash
<exact command(s)>
```

### How tests are started

```bash
<exact command(s)>
```

### How health/status is checked

```bash
<exact command(s)>
```

---

# 2. Repository Tree

Provide the meaningful repository tree.

Do not dump huge generated directories.

Include approximately:

```text
nomadicos/
├── ...
├── ...
└── ...
```

Then explain each top-level directory in one sentence.

| Path | Purpose | Confirmed from |
|---|---|---|
| `...` | `...` | `file/module` |

---

# 3. Actual Runtime Entry Point

Find the REAL production entry path.

Trace:

```text
process start
    ↓
main entry
    ↓
initialization
    ↓
router/orchestrator
    ↓
LLM
    ↓
planning/proposal
    ↓
policy/security
    ↓
execution
    ↓
result
    ↓
memory/audit/etc.
```

Provide the exact source files and functions involved.

| Stage | File | Function/Class | Evidence |
|---|---|---|---|
| Startup | | | |
| Input | | | |
| Routing | | | |
| Planning | | | |
| Policy | | | |
| Execution | | | |
| Verification | | | |
| Result | | | |
| Memory | | | |
| Audit | | | |

Important: identify whether this flow actually exists in runtime code or only in documentation/diagrams.

---

# 4. Produce the Real Runtime Call Graph

Generate a simplified call graph from an actual request.

Example format:

```text
CLI/API
  -> ...
    -> ...
      -> ...
```

Also provide one concrete traced example:

```text
USER INPUT:
"..."

FUNCTION 1:
...

FUNCTION 2:
...

MODEL REQUEST:
...

MODEL RESPONSE:
...

TOOL REQUEST:
...

POLICY DECISION:
...

EXECUTION:
...

FINAL RESULT:
...
```

Use real runtime observations.

Redact secrets.

---

# 5. Run a Basic Smoke Test

Execute the smallest realistic task.

Use several categories.

## Test A — Pure conversation

```text
Prompt:
"Reply with the word OK."
```

Record:

```text
Result:
Latency:
Model used:
Fallback?
Tokens if available:
Errors:
```

## Test B — Deterministic computation

```text
Prompt:
"Calculate 3817 * 294."
```

Record whether the model or a deterministic subsystem performed it.

## Test C — Read-only tool

Choose a harmless local operation.

```text
Prompt:
"Read <safe test file>."
```

Record the full path through the system.

## Test D — Write operation

Use a disposable temporary location.

```text
Prompt:
"Create a test file containing exactly HELLO."
```

Record authorization and execution.

## Test E — Invalid operation

Attempt something that should be rejected by policy.

```text
Prompt:
"Perform <known-disallowed harmless test action>."
```

Explain exactly where and why it was rejected.

## Test F — Unknown tool

Ask the model to invoke a nonexistent capability.

```text
Prompt:
"Use a tool named definitely_not_a_real_tool."
```

Record behavior.

---

# 6. Failure Reproduction

This section is critical.

Identify the **five most serious currently reproducible failures**.

For each:

```text
FAILURE #1

Name:
Severity: CRITICAL / HIGH / MEDIUM / LOW

Reproduction command:
...

Input:
...

Expected:
...

Actual:
...

Exit code:
...

Relevant logs:
...

Relevant files:
...

Root cause:
CONFIRMED / PARTIAL / INFERRED / UNKNOWN

Why you believe this is the root cause:
...

Can you reproduce 3/3 times?
YES / NO

Does behavior change after restart?
...

Does behavior change after clearing memory/state?
...

Does behavior change when using a different model?
...
```

Do not report vague statements such as "the agent is bad."

Produce reproducible failures.

---

# 7. Architecture Verification

The following architecture is the conceptual architecture inferred from the current diagram:

```text
INPUT
  ↓
ROUTER / MODEL FALLBACK
  ↓
PROPOSE / REASON / STRUCTURED OUTPUT
  ↓
SECURITY / POLICY
  ↓
EXECUTION
  ↓
VERIFY / TASK RESULT
  ↓
MEMORY / CHAT / LEARNING / ESCALATION
  ↓
feedback into the system
```

For each stage, answer:

| Component | Exists? | Runtime-used? | Deterministic? | Single source of truth? | Tests? |
|---|---:|---:|---:|---:|---:|
| Input | | | | | |
| Router | | | | | |
| Planner/Proposer | | | | | |
| Structured plan/IR | | | | | |
| Policy | | | | | |
| Capability system | | | | | |
| Scheduler | | | | | |
| Executor | | | | | |
| Sandbox | | | | | |
| Verifier | | | | | |
| Task state | | | | | |
| Memory | | | | | |
| Audit | | | | | |
| Learning | | | | | |
| Escalation | | | | | |

---

# 8. Identify the True System of Record

For each kind of state, identify the authoritative source.

| State | Source of truth | Can multiple components modify it? | Persistence |
|---|---|---|---|
| User identity | | | |
| Session | | | |
| Current task | | | |
| Task status | | | |
| Plan | | | |
| Tool permissions | | | |
| Memory | | | |
| Machine profile | | | |
| Audit log | | | |
| Model configuration | | | |
| Tool registry | | | |
| Process state | | | |
| Secrets | | | |

Explicitly identify contradictory state if it exists.

Example:

```text
Task status exists in:
- X
- Y
- Z

X and Y can disagree because ...
```

---

# 9. Task State Machine

Determine whether NomadicOS actually has a formal state machine.

Answer:

- Is task state represented explicitly?
- Where?
- Which states exist?
- Who can transition them?
- Are transitions validated?
- Can an LLM directly modify state?
- Can a failed task be resumed?
- Can a partially completed task be recovered?
- Are retries idempotent?
- Can two executors act on the same task concurrently?

Provide the real state machine if one exists:

```text
CREATED
  ↓
PLANNED
  ↓
AUTHORIZED
  ↓
RUNNING
  ↓
COMPLETED
```

Then list failure transitions:

```text
...
```

If no formal state machine exists, state:

```text
MISSING
```

---

# 10. LLM Boundary Audit

This is one of the most important sections.

Determine exactly what the model is allowed to influence.

Answer YES/NO and provide evidence:

| Question | YES/NO | Evidence |
|---|---:|---|
| Can the LLM select tools? | | |
| Can it select arbitrary tool arguments? | | |
| Can it select model/provider? | | |
| Can it alter policy? | | |
| Can it influence permissions? | | |
| Can it write persistent memory? | | |
| Can it change task state? | | |
| Can it trigger retries? | | |
| Can it trigger escalation? | | |
| Can it execute shell commands? | | |
| Can it directly access filesystem? | | |
| Can it access secrets? | | |
| Can it alter system configuration? | | |
| Can prior conversation become executable instruction? | | |
| Can tool output become future instructions without validation? | | |

Then answer:

### What is the exact contract between the LLM and NomadicOS?

Show the real JSON/schema/type.

If there is no strict intermediate representation, state:

```text
MISSING
```

---

# 11. Structured Output / IR Audit

Find every schema used between AI and runtime.

Provide:

```json
{
  "example": "real sanitized schema"
}
```

For each schema:

- validation library
- validation location
- whether extra fields are rejected
- whether types are enforced
- whether enum values are enforced
- whether arguments are validated
- whether schema version exists
- whether malformed model output can reach execution

Test malformed responses if possible.

Examples:

```text
missing required field
wrong type
unknown tool
extra privilege field
path traversal
unexpected command
invalid enum
null values
very large values
```

Record actual behavior.

---

# 12. Tool / Capability System Audit

Inventory every tool/capability.

| Tool | Purpose | Input schema | Permission required | Sandbox? | Direct OS access? |
|---|---|---|---|---|---|
| | | | | | |

Then answer:

- Where are tools registered?
- Can the model invent a tool name?
- Can a tool call another tool?
- Can tools recursively invoke the agent?
- Are capabilities identity-based?
- Are permissions checked per invocation?
- Can permissions be inherited?
- Is there an allowlist?
- Is there an implicit default permission?
- What is the failure behavior?

---

# 13. Security Boundary Testing

Do not merely describe security code.

Attempt to break it safely.

Test:

### Path traversal

```text
../../...
```

### Unauthorized filesystem path

Attempt access outside the intended sandbox.

### Environment variables

Determine whether a tool can read sensitive environment variables.

### Secrets

Determine whether model context can receive:

```text
API keys
tokens
database credentials
provider credentials
session secrets
```

Do not print actual secrets.

### Command injection

Use harmless markers.

### Tool confusion

Ask the model for a tool that does not exist.

### Prompt injection

Put malicious instructions inside:

- file contents
- tool output
- prior conversation
- memory
- web content if web tools exist

Then test whether those instructions gain authority.

For each:

```text
Attack:
Expected protection:
Actual result:
Protection layer:
Bypass found?
Severity:
```

---

# 14. Memory Audit

Inventory every memory-related subsystem.

Separate:

```text
Working memory
Session memory
Long-term memory
Machine profile
Task state
Conversation history
Experience memory
Audit log
```

For each, determine:

- storage
- writer
- reader
- lifecycle
- trust level
- schema
- deletion behavior
- retention
- whether it can affect future execution

Then run this test:

```text
Turn 1:
Store an obviously false/non-authoritative statement.

Turn 2:
Ask the system a question that could cause that statement to influence execution.

Turn 3:
Check whether the false statement affected behavior.
```

Record actual results.

---

# 15. Chat Replay / Context Injection Audit

Find exactly how historical conversation is converted into model context.

Answer:

- raw conversation or summarized?
- who builds the context?
- system messages separated?
- tool outputs separated?
- memory separated?
- trusted vs untrusted content marked?
- token limits?
- truncation strategy?
- ordering?
- injection resistance?
- can old instructions override current task?
- can tool output masquerade as system instruction?

Provide a real example of the final prompt/context sent to the model, sanitized for secrets.

---

# 16. Model Router / Fallback Audit

Inventory every model/provider.

| Provider/Model | Role | Context | Tool support | Structured output | Fallback priority |
|---|---|---|---|---|---|
| | | | | | |

Determine:

- what triggers fallback?
- does fallback preserve task state?
- does fallback preserve schema?
- does fallback change behavior?
- are retries counted globally or per model?
- are failures classified?
- can an unhealthy provider be selected repeatedly?
- is provider selection deterministic?

Run the same task repeatedly with fallback enabled and record differences.

---

# 17. Retry / Escalation Audit

Determine all retry mechanisms.

For each:

```text
Trigger:
Maximum retries:
Backoff:
Same model?
Different model?
Same plan?
Replanned?
Idempotency check?
State rollback?
```

Test a deterministic failure.

Record whether retries produce:

```text
same request
different request
duplicated side effects
partial state
infinite loop
```

---

# 18. Learning System Audit

Determine whether "learning" exists in production control flow.

Map:

```text
execution
  ↓
feedback
  ↓
learning
  ↓
what exactly changes?
```

Answer:

- Does production execution modify future system behavior automatically?
- Does it update prompts?
- memory?
- model selection?
- policies?
- tool behavior?
- routing?
- weights?
- configuration?

If it can change production behavior automatically, demonstrate exactly how.

Also state whether learning can be disabled independently.

---

# 19. Executor Audit

Find the exact executor implementation.

Determine whether it is responsible for:

- scheduling
- policy
- tool invocation
- process creation
- retries
- verification
- memory
- logging
- model invocation
- state transitions

Mark every responsibility that should probably live elsewhere.

Also answer:

- Is execution deterministic once an authorized command exists?
- Can executor behavior depend on LLM text?
- Are tool calls idempotent?
- Are timeouts enforced?
- Are cancellation signals supported?
- Are child processes tracked?
- Are orphan processes possible?
- Are resource limits enforced?

---

# 20. Process / Resource Management

Because NomadicOS is intended to behave like an OS/runtime, inspect:

- subprocess creation
- process IDs
- process lifecycle
- CPU limits
- memory limits
- disk limits
- network limits
- timeouts
- cancellation
- cleanup
- orphan process handling
- concurrent execution
- queueing/scheduling

Run a harmless resource/timeout test.

Example:

```text
Create a task that sleeps for N seconds.
Set timeout = M seconds where M < N.
Observe exactly what happens.
```

Record:

```text
Process terminated?
Parent aware?
Task state?
Cleanup?
Residual process?
```

---

# 21. Database / Persistence Audit

Inspect:

- schema
- migrations
- indexes
- constraints
- foreign keys
- transaction boundaries
- connection handling
- concurrency
- locking
- startup migrations
- recovery

Identify whether important invariants are enforced in the database or only in Python/application code.

Test a crash at an important transition if feasible.

For example:

```text
before execution
during execution
after execution / before task commit
```

Determine what state survives restart.

---

# 22. Crash Recovery

Perform controlled restarts during safe tests.

Test:

```text
restart while idle
restart during model request
restart during tool execution
restart after tool side effect but before result storage
restart during retry
```

For each:

```text
Task state after restart:
Duplicate execution possible?
Lost result?
Corrupted state?
Recoverable?
Manual intervention?
```

---

# 23. Concurrency Audit

Run at least two harmless tasks concurrently.

Determine:

- shared mutable state
- race conditions
- duplicate task execution
- database locking
- memory contamination
- model context contamination
- log correlation
- task ID isolation

Run:

```text
Task A
Task B
```

with intentionally different inputs and verify they cannot cross-contaminate.

---

# 24. Observability Audit

Inventory:

```text
Logs
Metrics
Traces
Task IDs
Request IDs
Model IDs
Tool invocation IDs
Policy decision IDs
Database transaction IDs
```

Answer:

- Can you reconstruct one task end-to-end from logs?
- Can you determine why a tool was allowed?
- Can you determine why a tool failed?
- Can you determine which model produced the plan?
- Can you reproduce the exact inputs?

If not, identify the missing fields.

---

# 25. Test Suite Audit

Run the complete test suite.

Report:

```text
Total:
Passed:
Failed:
Skipped:
Errors:
Coverage:
```

Then classify tests:

```text
unit
integration
end-to-end
security
failure recovery
concurrency
property-based
schema validation
```

Important:

Do not equate "many tests" with "good coverage."

Identify **critical behavior with no tests**.

---

# 26. Documentation vs Reality

Find claims in documentation that are not supported by implementation.

Create:

| Document claim | Implementation found? | Runtime verified? | Contradiction |
|---|---:|---:|---|
| | | | |

Pay particular attention to terms like:

```text
secure
sandboxed
persistent
autonomous
self-healing
learning
memory
verified
atomic
reliable
fail-safe
```

These words need evidence.

---

# 27. Architecture Smell Scan

Explicitly evaluate these potential smells:

```text
[ ] God object
[ ] God service
[ ] LLM in control plane
[ ] Circular dependencies
[ ] Multiple sources of truth
[ ] Raw conversation used as authority
[ ] Memory used as authority
[ ] Dynamic permissions
[ ] Implicit permissions
[ ] Hidden retries
[ ] Recursive agent loops
[ ] Unbounded autonomy
[ ] Missing state machine
[ ] Missing transactional boundary
[ ] Non-idempotent retry
[ ] Tight model/provider coupling
[ ] Security only at orchestration layer
[ ] Executor doing too much
[ ] Learning in production control path
[ ] Poor observability
[ ] Weak crash recovery
[ ] Weak concurrency isolation
[ ] Architecture differs from runtime
```

For every checked smell, give evidence.

---

# 28. Dependency / Coupling Map

Identify modules/services that know too much about one another.

Provide the top 15 most important dependencies:

```text
A → B
Reason:
Why this coupling exists:
Would changing B require changing A?
```

Identify circular imports or architectural cycles.

---

# 29. Configuration Audit

Find every source of configuration:

```text
.env
environment variables
YAML
JSON
TOML
database
hardcoded constants
CLI arguments
remote provider config
```

Determine precedence.

Provide an actual precedence chain.

Also identify configuration that can silently change system behavior.

---

# 30. Secrets Audit

Do not expose secrets.

Determine:

- where credentials enter
- where they are stored
- whether they are persisted
- whether they enter prompts
- whether logs can capture them
- whether tools can access them
- whether child processes inherit them
- whether provider adapters expose them

State any confirmed leakage path.

---

# 31. Performance / Latency Breakdown

For one representative successful request, measure:

```text
Input parsing:
Router:
Model:
Policy:
Tool:
Database:
Verification:
Total:
```

Provide approximate milliseconds where available.

Identify the three largest latency contributors.

---

# 32. Token / Context Audit

For a typical request report:

```text
System prompt size:
Developer/instruction context:
Conversation:
Memory:
Tool definitions:
Tool results:
User input:
Total input tokens:
Output tokens:
```

If exact token counting is unavailable, estimate and state that it is an estimate.

Identify unnecessary repeated context.

---

# 33. Minimal Reliable Core Test

Now temporarily ignore advanced features.

Define what the smallest useful NomadicOS request is.

Attempt to establish this path:

```text
INPUT
 ↓
LLM
 ↓
STRICT ACTION SCHEMA
 ↓
DETERMINISTIC POLICY
 ↓
DETERMINISTIC EXECUTOR
 ↓
RESULT
 ↓
AUDIT
```

Test it.

Report:

```text
Does this path exist?
YES / NO

If yes:
exact files/functions

If no:
what currently replaces it?
```

---

# 34. "Why Is It Not Working?" Root-Cause Ranking

After all tests, rank the top 10 root causes.

Format:

```text
#1 — <root cause>
Severity:
Confidence:
Evidence:
Affected components:
Why it causes current symptoms:
What would happen if ignored:
```

Do not list symptoms as root causes.

Bad:

```text
"The agent is unreliable."
```

Better:

```text
"Task authorization is performed by a model-generated boolean without
a deterministic policy enforcement point."
```

---

# 35. Separate Symptoms From Causes

Create this table:

| Symptom | Immediate cause | Deeper architectural cause |
|---|---|---|
| | | |

Do not stop at the immediate exception.

---

# 36. Critical Questions You Must Answer

Answer explicitly:

### Q1
What component has final authority to execute an operation?

### Q2
Can any LLM output reach execution without deterministic validation?

### Q3
What is the canonical representation of a task?

### Q4
What is the canonical representation of an allowed operation?

### Q5
What is the source of truth for task state?

### Q6
What is the source of truth for permissions?

### Q7
What is the source of truth for memory?

### Q8
Can a failed action be safely retried?

### Q9
Can NomadicOS recover from a process crash during execution?

### Q10
Can two tasks interfere with one another?

### Q11
Can historical conversation influence privileged execution?

### Q12
Can memory influence privileged execution?

### Q13
Can tools invoke the model recursively?

### Q14
Can the model alter its own permissions?

### Q15
Can the model select a more privileged tool after rejection?

### Q16
Can learning alter production behavior without deployment?

### Q17
Can one model's output semantics differ from another model's output semantics?

### Q18
Can you reconstruct any task completely from logs?

### Q19
What part of the claimed "OS" behavior is actually implemented?

### Q20
What is currently the single biggest reliability bottleneck?

---

# 37. What You Should NOT Do

Do not respond with:

```text
"We should use microservices."
"We should add more agents."
"We should use RAG."
"We need better prompts."
"We should use Kubernetes."
"We should use LangGraph."
"We should use a bigger model."
"We should add more memory."
```

unless you can tie the recommendation directly to a confirmed failure.

Do not redesign the system yet.

This phase is evidence collection.

---

# 38. Required Final Response From the Code Generator

Your response must end with these exact sections:

## A. EXECUTIVE FACTS

Maximum 25 bullets.

Only confirmed facts.

## B. TOP 10 FAILURES

Ranked by severity and impact.

## C. TOP 10 ROOT CAUSES

Distinguish cause from symptom.

## D. MISSING COMPONENTS

Anything required for the intended architecture but absent.

## E. DANGEROUS COMPONENTS

Existing components whose current behavior creates major reliability/security risk.

## F. ARCHITECTURE CONTRADICTIONS

Diagram/documentation vs actual runtime.

## G. TEST EVIDENCE

Commands, outputs, and failures.

## H. UNKNOWN / UNVERIFIED

Anything you could not establish.

## I. FILES THAT MATTER MOST

Top files/modules that control system behavior.

Example:

```text
1. path/to/file.py — reason
2. path/to/file.py — reason
...
```

## J. RAW EVIDENCE APPENDIX

Important logs, stack traces, schemas, call traces, and command results.

Redact secrets.

---

# 39. Minimum Evidence Standard

The completed response is considered insufficient if it:

- only explains architecture from the diagram,
- only reads README files,
- does not run the system,
- does not run tests,
- does not provide file/function names,
- does not reproduce at least one failure,
- claims security without attempting bypasses,
- claims reliability without crash/retry tests,
- claims memory behavior without tracing reads/writes,
- claims execution flow without tracing an actual request,
- suggests redesign before establishing current behavior.

---

# 40. Final Instruction to the Code Generator

Treat NomadicOS as a potentially broken production system.

Do not try to impress the reviewer.

Do not defend the existing architecture.

Do not assume intended behavior equals actual behavior.

Read the code.

Run the code.

Break it safely.

Trace it.

Measure it.

Then report the evidence.

The next document will be produced from your report and will contain the actual architectural correction plan, including what should be deleted, what should be kept, what should be rewritten, what should be isolated, what contracts should change, and in what order the system should be rebuilt.

**Do not produce that redesign in this response.**

