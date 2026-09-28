# NomadicBrain — Master Implementation Specification

## 0. Purpose

This document is the authoritative implementation specification for building **NomadicBrain** inside **NomadicOS**.

NomadicBrain is the cognitive-control system that sits above models and capabilities. It is **not a model**, **not a tool executor**, and **not a replacement for the NomadicOS security/runtime boundaries**.

The objective is to turn NomadicOS from a model-mediated execution runtime into a coherent, stateful, goal-directed cognitive system with:

- structured perception
- attention and context selection
- a world model
- persistent memory
- hierarchical task decomposition
- candidate action generation
- semantic and deterministic monitoring
- state prediction
- action evaluation
- bounded search
- deterministic orchestration
- independent verification
- bounded recovery
- trajectory learning and replay
- model-agnostic cognitive routing
- capability/skill routing
- adaptive compute
- complete observability

The first implementation target is a **deterministic cognitive substrate**, not a powerful autonomous agent.

---

# 1. Non-Negotiable Architecture

## 1.1 Brain ≠ Model

Models are replaceable reasoning engines.

Current default:

```text
Ornith 1.5 9B
```

Possible alternatives:

```text
gpt-oss:20b
Gemma
Qwen
other local models
cloud models
future models
```

NomadicBrain must not contain permanent model-specific logic.

Bad:

```python
if model == "ornith":
    ...
```

Good:

```python
cognitive_engine.run(
    role="predictor",
    state=state,
    requirements=requirements,
)
```

Model selection belongs to the cognitive/model router.

---

## 1.2 Cognition ≠ Execution

The model and cognitive modules can propose.

The runtime executes.

```text
Cognitive proposal
      ↓
Typed ActionCandidate
      ↓
Validation
      ↓
Authorization / Policy
      ↓
AuthorizedAction
      ↓
Executor
```

No language-model output can directly produce a side effect.

---

## 1.3 Evaluation ≠ Verification

Evaluation estimates the usefulness of a predicted future.

Verification checks reality.

```text
Evaluator
    =
"How good does this predicted state look?"

Verifier
    =
"Did the requested state actually happen?"
```

A model saying `done` is never proof.

A tool returning exit code `0` is not automatically proof of goal completion.

---

## 1.4 Memory ≠ Context Window

The context window is temporary inference input.

Memory is persistent system state.

Memory must survive:

- model changes
- process restarts
- context truncation
- new sessions
- worker replacement

---

## 1.5 Hard Controls Are Deterministic

The following must never rely solely on model output:

- authorization
- scope
- permissions
- capability existence
- resource limits
- action identity
- retry limits
- persistence
- rollback
- verification wiring
- audit records

---

## 1.6 Every Important Decision Must Be Replayable

Every mission must produce a machine-readable trajectory.

At minimum:

```text
goal
state
memory used
model assignments
candidate actions
monitor decisions
predictions
evaluation
selected action
execution
observation
verification
failure/recovery
memory updates
```

---

# 2. Current NomadicOS Baseline

The live repository is:

```text
https://github.com/dhruv-motaval/NomadicOS
```

Default branch:

```text
master
```

Repository baseline verified during specification authoring:

```text
NomadicOS v0.2.0
Python 3.12+
LangGraph orchestration
llama.cpp runtime
Ollama runtime
typed kernel contracts
model registry
deterministic model routing
Action IR
owner authority
policy/authorization
filesystem execution
terminal execution
process supervision
goal verification
coding worker
critic/evaluator worker
bounded recovery
evidence-backed completion semantics
PostgreSQL hooks
```

Current repository layout includes:

```text
src/nomadicos/
├── action_ir/
├── agents/
├── authority/
├── cli/
├── contracts/
├── evaluation/
├── executor/
├── inference/
├── kernel/
├── orchestration/
├── registry/
├── router/
├── tools/
└── verification/

tests/
├── agents/
├── critic/
├── contracts/
├── executor/
├── hardware/
├── inference/
├── kernel/
├── orchestration/
├── registry/
├── security/
└── verification/
```

Existing strengths that must be preserved:

```text
model output is not raw execution
authority is isolated
executor consumes authorized actions
goal verification is evidence-backed
recovery is bounded
production paths must not call inference directly
```

Treat the repository's current implementation as the source of truth for what already exists.

Do not assume a feature is implemented because this specification describes it.

---

# 3. Repository State Must Be Checked Every Session

Before every phase and every new implementation session:

1. Inspect the latest `master`.
2. Inspect recent commits.
3. Inspect the current working branch.
4. Inspect relevant source and tests.
5. Run the relevant baseline tests.
6. Determine which phase is actually complete.
7. Do not trust old progress statements over current repository evidence.

Use the repository as the **live state ledger**.

The implementation assistant must report:

```text
CURRENT COMMIT
CURRENT BRANCH
PHASE IN PROGRESS
PHASES VERIFIED COMPLETE
FILES CHANGED
TESTS RUN
TEST RESULTS
REMAINING WORK
```

Never claim completion without evidence.

---

# 4. Complete Brain Architecture

```text
                         USER / WORLD
                              │
                              ▼
                         PERCEPTION
                              │
                              ▼
                           ATTENTION
                              │
                              ▼
                         WORLD MODEL
                              │
                              ▼
                            MEMORY
                              │
                              ▼
                             GOAL
                              │
                              ▼
                       TASK DECOMPOSER
                              │
                              ▼
                          TASK GRAPH
                              │
                              ▼
                            ACTOR
                              │
                         candidates
                              ▼
                           MONITOR
                        ↙           ↘
                    reject           accept
                      │                 │
                      └──── feedback    ▼
                                   PREDICTOR
                                        │
                                   predictions
                                        ▼
                                    EVALUATOR
                                        │
                                        ▼
                                 SEARCH / SELECTOR
                                        │
                                        ▼
                                  ORCHESTRATOR
                                        │
                                        ▼
                              CAPABILITY RESOLUTION
                                        │
                                        ▼
                              POLICY / AUTHORITY
                                        │
                                        ▼
                                     EXECUTOR
                                        │
                                        ▼
                                      WORLD
                                        │
                                        ▼
                                   OBSERVATION
                                        │
                                        ▼
                                    VERIFIER
                                  ↙         ↘
                              failure       success
                                │              │
                                ▼              ▼
                            RECOVERY        COMPLETE
                                │
                                ▼
                         REPLANNING CYCLE
                                │
                                ▼
                         PREDICTION ERROR
                                │
                                ▼
                              LEARNING
                                │
                                ▼
                             MEMORY
                                │
                                └──────────────↺
```

Cross-cutting services:

```text
Model Routing
Capability Routing
Budgeting
Uncertainty
Meta-control
Tracing
Replay
Benchmarking
Security policy
```

---

# 5. Core State Model

The initial canonical state is:

```python
class BrainState:
    goal: Goal
    world: WorldState
    task_graph: TaskGraph
    active_subgoal: Subgoal | None
    working_memory: WorkingMemory
    relevant_memories: list[MemoryRef]
    hypotheses: list[Hypothesis]
    observations: list[Observation]
    evidence: list[Evidence]
    capabilities: list[Capability]
    constraints: Constraints
    budget: BudgetState
    history: list[EventRef]
```

Canonical objects:

```text
Goal
Subgoal
TaskNode
ActionCandidate
Prediction
Evaluation
Observation
Evidence
Hypothesis
VerificationResult
Failure
RecoveryPlan
Decision
```

Initial hard invariant:

```text
Conversation
    ↓
State Builder
    ↓
BrainState
```

No cognitive module should treat raw conversation text as authoritative system state.

---

# 6. Event and State Rules

Every state object must have:

- deterministic serialization
- explicit schema/version
- stable identity
- timestamps where applicable
- provenance where applicable
- controlled mutation semantics

Every event should have:

```text
event_id
mission_id
timestamp
event_type
actor
parent_event
payload
schema_version
```

Prefer an event-driven state transition model:

```text
BrainState_t
     +
Event
     ↓
State reducer
     ↓
BrainState_t+1
```

Do not create a giant mutable object that every component can modify arbitrarily.

---

# 7. World State

World state represents what the system currently believes about the external environment.

It must distinguish:

```text
FACT
UNKNOWN
HYPOTHESIS
PREDICTION
VERIFIED FACT
STALE FACT
```

Example:

```text
FACT:
API returned HTTP 500

FACT:
database connection failed

UNKNOWN:
database process is actually down

HYPOTHESIS:
latest deployment changed database configuration
```

World state should support relationships:

```text
project
  → service
      → endpoint
          → function
              → dependency
```

For controlled security workflows:

```text
authorized target
  → asset
      → service
          → endpoint
              → evidence
                  → hypothesis
```

---

# 8. Belief State / Uncertainty

NomadicBrain must explicitly support uncertain world models.

Add:

```text
BeliefState
```

with:

```text
hypotheses
confidence
supporting_evidence
conflicting_evidence
uncertainty
last_updated
expected_information_gain
```

Do not reduce uncertain reality to a single asserted state.

Example:

```text
Database failure cause:

config error      0.55
service outage    0.30
network problem   0.15
```

Belief updates must remain attributable to new observations/evidence.

---

# 9. Attention

Attention controls what the cognitive model actually receives.

Input:

```text
all available observations
all relevant memory candidates
current goal
active subgoal
uncertainty
risk
recent changes
```

Output:

```text
focused cognitive context
```

Conceptually:

```text
10,000 observations
        ↓
     salience
        ↓
 context selection
        ↓
  BrainState view
```

Attention should consider:

- goal relevance
- novelty
- uncertainty
- contradiction
- risk
- recency
- dependency impact
- expected information gain

Attention selection must be benchmarked.

---

# 10. Memory Architecture

Implement five logical memory classes.

## Working Memory

Current:

```text
mission
subgoal
recent observations
active hypotheses
current plan
current constraints
```

## Episodic Memory

Historical trajectories:

```text
goal
→ actions
→ observations
→ failures
→ recovery
→ outcome
```

## Semantic Memory

Stable knowledge:

```text
facts
documentation
architecture
concepts
domain knowledge
```

## Procedural Memory

Reusable methods:

```text
skills
workflows
successful action sequences
repair patterns
```

## Graph Memory

Relationships:

```text
entities
events
dependencies
causal hypotheses
historical transitions
```

Every persisted memory item should carry:

```text
source
timestamp
confidence
supersession status
evidence references
```

Stale memory must be supersedable.

---

# 11. Memory Retrieval

Canonical retrieval:

```text
Current Goal
     ↓
Retrieval Query
     ↓
Semantic Retrieval
+ Episodic Retrieval
+ Graph Retrieval
     ↓
Rerank
     ↓
Relevance Filter
     ↓
Focused Memory View
     ↓
BrainState
```

Memory must not be a firehose.

The retrieval layer should expose:

```text
why this memory was retrieved
which source supports it
confidence
age
whether superseded
```

---

# 12. Goal System

A Goal must have:

```text
goal_id
description
priority
constraints
deadline (optional)
success predicates
failure conditions (optional)
parent goal (optional)
```

A goal without testable completion predicates should not silently become `SUCCESS`.

Goal hierarchy:

```text
Goal
 ├── Subgoal
 │    ├── TaskNode
 │    └── TaskNode
 └── Subgoal
```

The system must support:

```text
pause
resume
cancel
replan
supersede
```

Add goal arbitration before production autonomy.

---

# 13. Task Decomposer

Purpose:

> What must be achieved?

Input:

```text
Goal
WorldState
RelevantMemory
Constraints
BeliefState
```

Output:

```text
TaskGraph / DAG
```

Rules:

- measurable subgoals
- explicit dependencies
- no circular dependencies
- explicit success predicates
- hierarchical decomposition for difficult tasks

The Decomposer does not select tools.

The Decomposer does not execute.

Initial default model may be Ornith, but the contract must remain model-agnostic.

---

# 14. Actor

Purpose:

> What could I do next?

The Actor produces multiple `ActionCandidate`s.

Example:

```text
Action A
Action B
Action C
```

Candidate:

```json
{
  "action_id": "a1",
  "capability": "...",
  "tool": "...",
  "arguments": {},
  "preconditions": [],
  "expected_effect": ""
}
```

Actor must not execute.

Actor may generate:

```text
EXECUTE
OBSERVE
QUERY
EXPERIMENT
WAIT
```

This is important because gathering information can be better than immediately changing the world.

---

# 15. Monitor

Purpose:

> Is the proposed action valid for the current state?

Two stages:

## Semantic monitor

Checks:

```text
consistency
relevance
missing prerequisites
hallucinated assumptions
plan conflict
contradictions
```

## Deterministic monitor

Checks:

```text
schema
capability existence
object existence
dependency satisfaction
policy
scope
resource limits
action identity
```

Both must pass before an executable action reaches the execution layer.

---

# 16. Action Proposal Loop

Canonical loop:

```text
Actor
  ↓
Candidate actions
  ↓
Monitor
  ├── reject → structured feedback → Actor
  └── accept
```

Requirements:

- bounded retries
- structured feedback
- deterministic rejection conditions
- no authority escalation through feedback manipulation

---

# 17. Predictor

Purpose:

> What is likely to happen?

Input:

```text
WorldState
ActionCandidate
BeliefState
```

Output:

```json
{
  "predicted_state": {},
  "confidence": 0.0,
  "assumptions": [],
  "uncertainties": []
}
```

The Predictor should be allowed to return multiple futures.

Example:

```text
Action A
 ├── state X   0.70
 └── state Y   0.30
```

Track prediction provenance and model identity.

---

# 18. Evaluator

Purpose:

> How useful is each predicted future for the current goal?

Inputs may include:

```text
goal progress
goal distance
risk
confidence
resource cost
latency
reversibility
evidence quality
deadline
```

Evaluation is a decision aid, not truth.

---

# 19. Search

Initial search parameters:

```text
branching = 2
depth = 2
```

Adaptive policy:

```text
Easy            → shallow
Normal          → moderate
High uncertainty→ deeper
Critical        → deeper + stronger verification
```

Never allow unbounded search.

Each search branch must be reproducible.

Cache only when cache validity can be established from:

```text
state fingerprint
action fingerprint
world version
model version
policy version
environment version
```

---

# 20. Selector

The Selector converts evaluated candidate futures into a Decision.

It must explicitly account for:

```text
goal progress
risk
uncertainty
cost
reversibility
policy state
deadline
```

Do not reduce all decision-making to a single opaque numeric score without retaining the component evidence.

---

# 21. Orchestrator

The Orchestrator is deterministic runtime control.

Responsibilities:

```text
task graph scheduling
dependency resolution
budget accounting
retry limits
cancellation
action commitment
subgoal transition
recovery entry
completion detection
```

The LLM may recommend.

The runtime commits state transitions.

---

# 22. Action Commitment Gate

Before side effects:

```text
Decision
   ↓
Action Commitment Gate
```

The action contract should expose:

```text
reversible?
destructive?
external_side_effect?
requires_confirmation?
rollback_strategy?
resource_lock?
transaction_id?
authorization_reference?
```

This gate prevents a semantically valid action from becoming an uncontrolled side effect.

---

# 23. Capability / Skill Architecture

Capabilities must be typed.

Example:

```json
{
  "name": "inspect_dependency_graph",
  "description": "...",
  "inputs": {},
  "outputs": {},
  "requirements": [],
  "risk": "medium",
  "verification": {}
}
```

Skill lifecycle:

```text
Skill Registry
      ↓
Skill Retrieval
      ↓
Compatibility Check
      ↓
Capability Selection
      ↓
Actor
      ↓
Monitor
      ↓
Execution
      ↓
Verification
```

Skills must be discoverable by capability and contract, not by filename assumptions.

---

# 24. Security Boundary

NomadicBrain may perform deep reasoning, but authorization must remain outside model authority.

Execution path:

```text
Scope
 ↓
Policy
 ↓
Capability
 ↓
Sandbox
 ↓
Tool
```

The brain must not be able to create its own permission.

External text is untrusted data:

```text
repository contents
documents
browser content
tool output
logs
test output
memory
```

None of these can grant authorization merely by containing an instruction.

For security workflows add:

```text
EngagementScope
TargetRegistry
SandboxState
SecurityCapability
EvidenceStore
AuditRecord
```

---

# 25. Executor Integration

The executor remains responsible only for execution.

It must not:

- choose models
- grant permissions
- modify policy
- decide the overall goal result
- call the LLM to decide what it should execute

The executor consumes authorized actions and emits evidence.

---

# 26. Observation Layer

Every execution must generate structured observations.

Examples:

```text
exit code
stdout
stderr
filesystem diff
HTTP result
browser state
screenshot state
process state
artifact state
```

Normalize them into:

```text
Observation
```

Then update:

```text
WorldState
BeliefState
History
Evidence
```

---

# 27. Verification

Verification uses explicit predicates.

Examples:

```text
file_exists
directory_exists
file_content_equals
file_contains
file_sha256
artifact_exists
exit_code_equals
stdout_contains
stdout_equals
stderr_contains
API response predicates
behavior predicates
```

Goal verification:

```text
Goal
 ↓
Completion Predicates
 ↓
Runtime Evidence
 ↓
Predicate Evaluation
 ↓
Goal Aggregation
 ↓
VerificationResult
```

Allowed terminal outcomes:

```text
SUCCESS
PARTIAL
FAILED
BLOCKED
NOT_VERIFIED
```

Never convert:

```text
plan exhausted
model says done
tool returned exit 0
```

into goal SUCCESS without evidence.

---

# 28. Failure Model

Normalize failures.

Example classes:

```text
MODEL_FAILURE
SCHEMA_FAILURE
CAPABILITY_FAILURE
AUTHORIZATION_FAILURE
POLICY_FAILURE
EXECUTION_FAILURE
TIMEOUT
ENVIRONMENT_FAILURE
VERIFICATION_FAILURE
PREDICTION_FAILURE
DEPENDENCY_FAILURE
UNKNOWN_FAILURE
```

Failure signatures must be normalized so equivalent failures are recognized.

---

# 29. Recovery

Canonical recovery loop:

```text
Failure
  ↓
Failure Classifier
  ↓
Root Cause Hypotheses
  ↓
Recovery Planner
  ↓
Candidate Actions
  ↓
Monitor
  ↓
Predict
  ↓
Evaluate
  ↓
Execute
  ↓
Verify
```

Recovery must have:

```text
attempt budget
time budget
state budget
loop detection
terminal failure state
```

No infinite self-repair.

---

# 30. Learning

Persist:

```text
successful trajectories
failed trajectories
recovery trajectories
prediction errors
verification outcomes
model outcomes
skill outcomes
```

Trajectory structure:

```text
Mission
 ├── InitialState
 ├── Goal
 ├── Decisions
 ├── Actions
 ├── Observations
 ├── Verifications
 ├── Failures
 ├── Recoveries
 └── FinalState
```

---

# 31. Replay

Replay should reconstruct the decision trajectory.

```text
trajectory
 ↓
replay
 ↓
compare against baseline
 ↓
identify regression/improvement
 ↓
benchmark
 ↓
canary
 ↓
promote
```

Replay must preserve:

```text
model versions
prompt/template version
schema version
policy version
tool version
world/state version
```

---

# 32. Model Routing

The model layer exposes capabilities such as:

```text
planning
coding
tool_use
reasoning
prediction
vision
security_analysis
latency
memory_cost
reliability
```

Do not use invented benchmark values.

Populate routing metadata only from measured evidence.

Role-based routing:

```text
Decomposer → measured planning model
Actor      → measured tool/coding model
Monitor    → model + deterministic checks
Predictor  → measured predictor
Evaluator  → measured evaluator/reasoner
Verifier   → deterministic verifier
Orchestrator → Python/runtime
```

---

# 33. Adaptive Compute

Effort levels:

```text
REFLEX
NORMAL
DEEP
CRITICAL
```

Escalation triggers:

```text
uncertainty
risk
contradiction
failure
verification failure
model disagreement
task complexity
deadline
```

Example:

```text
NORMAL
  ↓
uncertainty high
  ↓
DEEP
  ↓
verification failure
  ↓
CRITICAL
```

---

# 34. Metacognition

Add a MetaController that reasons about the reasoning process.

Responsibilities:

```text
confidence
uncertainty
contradiction detection
stuck detection
strategy switching
compute escalation
model disagreement
information gathering
```

This is distinct from ordinary task reasoning.

---

# 35. Information-Gathering Controller

NomadicBrain must be able to choose:

> Learn more before acting.

Possible action types:

```text
OBSERVE
QUERY
INSPECT
EXPERIMENT
EXECUTE
WAIT
```

The controller should estimate:

```text
expected information gain
cost of gathering information
risk of acting without information
```

---

# 36. Goal Arbitration

Support competing goals.

Example:

```text
goal A: finish task
goal B: preserve security policy
goal C: avoid destructive change
goal D: meet deadline
```

The Goal Manager should manage:

```text
priority
deadline
importance
dependency
conflict
interrupt
suspend
resume
cancel
```

Never allow an incidental subgoal to silently override a higher-level constraint.

---

# 37. Concurrency / Multi-Mission Control

Production NomadicOS will eventually have multiple missions.

Support:

```text
mission isolation
resource locks
leases
cancellation
preemption
stale observations
parallel branches
race protection
```

Every mission needs its own:

```text
mission_id
BrainState
event stream
trajectory
resource scope
policy context
```

---

# 38. Trace Schema

Every cognitive cycle should emit something equivalent to:

```json
{
  "mission_id": "...",
  "cycle": 12,
  "goal": "...",
  "subgoal": "...",
  "state_version": "...",
  "model_assignments": {},
  "candidates": [],
  "monitor_results": [],
  "predictions": [],
  "evaluation": {},
  "selected_action": {},
  "execution_result": {},
  "verification": {},
  "prediction_error": {},
  "memory_updates": []
}
```

Trace data must be machine-readable and replayable.

---

# 39. Benchmarking

Build benchmark families for:

```text
state construction
memory retrieval
decomposition
action validity
prediction
evaluation
search
goal verification
recovery
model routing
skill routing
end-to-end mission execution
```

Important comparisons:

```text
brain OFF
vs
brain ON
```

and:

```text
single model
vs
model routed by cognitive role
```

and:

```text
without search
vs
bounded search
```

Every architectural change must have measurable verification.

---

# 40. Implementation Sprints

## Sprint 1 — Foundation

Implement:

```text
brain/state.py
brain/schemas.py
brain/contracts.py
brain/events.py
brain/errors.py

planner/task_graph.py
planner/orchestrator.py
```

Tests:

```text
tests/brain/test_state.py
tests/brain/test_task_graph.py
tests/brain/test_contracts.py
```

Invariant:

```text
Goal
 ↓
BrainState
 ↓
TaskGraph
 ↓
Deterministic Orchestrator
```

Do not add powerful model reasoning yet.

---

## Sprint 2 — MAP Core

Implement:

```text
decomposer
actor
monitor
structured action candidates
Actor ↔ Monitor loop
```

Use one real NomadicOS workflow as the vertical integration target.

---

## Sprint 3 — Deliberation

Implement:

```text
predictor
evaluator
bounded search
selector
prediction cache
```

Add deterministic search/replay tests.

---

## Sprint 4 — Reality Loop

Integrate:

```text
ActionCandidate
 ↓
existing validation
 ↓
existing authority
 ↓
existing executor
 ↓
Observation
 ↓
Verifier
```

Add failure classification and recovery.

Do not bypass existing security/runtime boundaries.

---

## Sprint 5 — Memory

Implement:

```text
working memory
episodic memory
semantic retrieval
graph memory
trajectory store
provenance
supersession
```

---

## Sprint 6 — Adaptive Intelligence

Implement:

```text
attention
belief state
uncertainty
metacognition
information gathering
adaptive search
adaptive effort
```

---

## Sprint 7 — Model Independence

Implement:

```text
model registry
capability benchmark
role-based routing
model canary
replay-based promotion
```

---

## Sprint 8 — Skills

Implement:

```text
capability registry
skill retrieval
compatibility checks
skill adapters
structured skill outputs
```

---

## Sprint 9 — Security Research Lane

Implement:

```text
engagement scope
target registry
sandbox manager
security capability taxonomy
evidence store
security benchmark suite
audit boundary
```

Keep execution authorization deterministic.

---

## Sprint 10 — Production

Implement:

```text
soak tests
fault injection
stress tests
performance profiling
model failover
benchmark regression gates
concurrency controls
continuous improvement
```

---

# 41. Definition of Done

NomadicBrain is not complete until it can:

1. Accept a high-level goal.
2. Construct structured world state.
3. Represent uncertainty.
4. Retrieve relevant persistent memory.
5. Decompose goals into valid task graphs.
6. Generate multiple candidate actions.
7. Generate information-gathering actions.
8. Reject invalid candidates before execution.
9. Predict multiple possible futures.
10. Evaluate those futures.
11. Perform bounded search.
12. Select an action using explicit criteria.
13. Execute through existing NomadicOS authority/execution boundaries.
14. Observe actual world state.
15. Verify the result independently.
16. Recover from failure.
17. Update world state and memory.
18. Measure prediction error.
19. Replay trajectories.
20. Route cognitive roles to measured models.
21. Replace models without redesigning the brain.
22. Select skills dynamically.
23. Support multiple missions safely.
24. Survive model/provider/tool failures.
25. Produce complete machine-readable traces.
26. Pass benchmark gates before promotion.
27. Improve from trajectories without silently modifying production behavior.
28. Never claim success without evidence.

---

# 42. First Implementation Task

**Do only Sprint 1.**

Before writing code:

```text
1. Inspect the current repository.
2. Inspect current contracts.
3. Inspect current orchestration state.
4. Inspect existing action/authority/executor/verification boundaries.
5. Identify exact integration points.
6. Do not duplicate existing functionality.
```

Create only the minimum foundation necessary for:

```text
Goal
 ↓
BrainState
 ↓
TaskGraph
 ↓
Deterministic Orchestrator
```

Add tests.

Run:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Run narrower tests first when appropriate, then the full available suite.

Do not claim Sprint 1 complete until tests and repository inspection support the claim.

---

# 43. Phase Progress Protocol

When asked:

```text
next prompt
```

do not regenerate the whole specification.

Instead:

1. Inspect the latest repository state.
2. Determine the first incomplete sprint.
3. Summarize the current evidence.
4. Produce **one implementation prompt for that sprint only**.
5. Do not skip incomplete dependencies.
6. Include exact files/components to inspect.
7. Include tests and verification commands.
8. Include explicit stop conditions.
9. Require a final implementation report.

Every next prompt must be repository-state-aware.

---

# 44. Required Future Prompt Format

Every future phase prompt must contain:

```text
ROLE
CURRENT REPOSITORY STATE
PHASE
OBJECTIVE
EXACT COMPONENTS TO INSPECT
IMPLEMENTATION REQUIREMENTS
INVARIANTS
SECURITY CONSTRAINTS
TEST REQUIREMENTS
VERIFICATION COMMANDS
STOP CONDITIONS
FINAL REPORT FORMAT
```

The coding agent must inspect existing code before modifying it.

Never assume a file exists.

Never invent an interface without checking the repository.

---

# 45. Final Engineering Doctrine

```text
Build the contracts before the intelligence.

Build the state before the planning.

Build verification before autonomy.

Build evidence before learning.

Build routing from measurements, not model reputation.

Build capabilities behind typed boundaries.

Keep authority deterministic.

Keep execution deterministic.

Keep memory attributable.

Keep recovery bounded.

Keep important decisions replayable.

Keep NomadicBrain model-agnostic.
```

The goal is not to create a larger prompt loop.

The goal is to create a **persistent cognitive control system around interchangeable models and controlled capabilities**.
