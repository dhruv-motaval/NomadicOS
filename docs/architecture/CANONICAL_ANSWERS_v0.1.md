# Canonical Answers — NomadicOS v0.1 Architecture Questions

- **Date received:** 2026-09-04
- **Provenance:** Owner-provided answers produced by GPT-5.6 Luna, reviewing
  `NomadicOS_Canonical_Blueprint_Analysis.md` against `NomadicOS_v0.1_Canonical_Blueprint.md`
- **Status:** AUTHORITATIVE — the framework is confirmed. Recorded verbatim below
  (formatting normalized). Codified in ADR-0001 … ADR-0027.

> These decisions resolve the open questions in the analysis report.
> Do not redesign the entire architecture from these answers.
> Use them to update the existing blueprint and generate ADRs.

## A. Local Model Runtime & Models

**Q1 — Local inference runtime.** Do a short runtime spike first, but make
llama-cpp-python the preferred first implementation target for the local model adapter.
Local/in-process execution fits the local-first design; good control over model
loading/unloading; suitable for GGUF-style local models; avoids a model-server dependency.
Implement `LocalModel` as an abstraction first; do not spread llama-cpp-python-specific
code throughout the Agent Runtime. Later runtimes remain possible through the same
interface. → **ADR-0001**

**Q2 — Minimum hardware profile.** Do not define a hard single-GPU requirement.
Support CPU fallback, GPU acceleration when available, dynamic RAM/VRAM detection.
Build a HardwareProfile subsystem reporting CPU, RAM, GPU, VRAM, architecture, OS;
model selection uses this profile. Do not promise every model runs on every machine.
→ **ADR-0002**

**Q3 — Canonical embedding model.** Do not permanently lock the embedding model before
benchmarking. Initial recommendation: a relatively small local embedding model around
384 dimensions. Make the embedding model configurable and versioned; every vector index
must record embedding model, version, dimensions, distance metric. Select the final
canonical model after the local retrieval benchmark. → **ADR-0003**

**Q4 — Vision interface / Gemma.** Use a dedicated VisionModel capability interface.
Gemma-family local vision model is the first implementation candidate. Vision is a
capability, not necessarily a property shared by every local model. A multimodal
LocalModel may satisfy VisionModel. Keep vision implementation replaceable; do not
hard-code Gemma throughout the Agent Runtime. → **ADR-0004**

**Q5 — Model acquisition and integrity.** Hugging Face / direct publisher sources
initially. Require source metadata, version, checksum/hash verification where available,
local registration, model validation before activation. Do not automatically execute
model-provided code; models are artifacts until validated. → **ADR-0005**

**Q6 — Multiple models resident simultaneously.** Sequential model residency is the
default v0.1 behavior; multiple simultaneously loaded models only when hardware/resources
permit. Default: Model A → unload/swap → Model B. Model Manager exposes load, unload,
health, resource requirements; ModelSelector considers loading cost. → **ADR-0006**

## B. Data & Storage

**Q7 — Native Vector Engine V0.** Build our own exact-search baseline first. Do NOT make
pgvector/Qdrant/LanceDB the production memory backend — external vector systems are
experimental comparison backends only. Flow: vectors → exact search → ground truth →
ANN → Recall@K comparison. Do not implement HNSW before exact-search benchmarks exist.
→ **ADR-0007**

**Q8 — Vector data storage.** Dedicated local vector-data directory under
`NOMADICOS_DATA_DIR`: `data/vector/<index-id>/`. Store format_version, dimensions,
metric, embedding model/version, index metadata. V0 uses simple local snapshot
structures. Do not make the format permanent until V0 experimentation completes.
→ **ADR-0008**

**Q9 — PostgreSQL deployment.** Docker Compose for development. Production/local-user
installation eventually supports a setup abstraction rather than hard-coding Docker.
Create `docker-compose.dev.yml`. Do not put PostgreSQL data in Git; do not keep database
files under OneDrive/sync-heavy directories. → **ADR-0009**

**Q10 — PostgreSQL driver.** psycopg 3 — modern support, good async, works well with
Python. Keep PostgreSQL access behind repositories/domain services; agents/models never
receive arbitrary SQL access. → **ADR-0010**

**Q11 — Backup implementation.** pg_dump-based logical backups for v0.1; vector data gets
its own versioned snapshot/backup mechanism. BackupManager: create, verify, restore,
record version, test restore. → **ADR-0011**

## C. Architecture & Process Model

**Q12 — Process model.** Single main Python process with asyncio for v0.1; supervised
subprocesses only where the workload requires them. Multi-process IPC is unnecessary
complexity too early. Long-running/high-risk operations are isolated behind a process
execution abstraction so subprocess isolation can be added without redesigning the
Agent Runtime. Design process boundaries now; implement conservatively. → **ADR-0012**

**Q13 — Constitution representation.** Two layers: (1) immutable security invariants in
code; (2) versioned YAML policies for user-configurable behavior. Implement
`constitution/invariants.py`, `constitution/policy_schema.py`,
`constitution/policy_loader.py` and `config/policies/*.yaml`. Policy files are schema
validated; invalid policy = fail closed. → **ADR-0013**

**Q14 — UI / API.** CLI-first for v0.1; local web UI/dashboard after the core runtime
works. Internal HTTP API not required for the first runtime if direct in-process
interfaces suffice; if introduced, it must be authenticated/authorized. Do not make
frontend work block the Agent Runtime. → **ADR-0014**

**Q15 — Task intake.** CLI-first: `nomadicos task create "Open VS Code and run the
tests..."`. Deterministic, easy to test and automate. The task CLI talks to the same
Agent Runtime interface the future UI will use. → **ADR-0015**

**Q16 — Watchdog and emergency stop.** Watchdog: in-process supervisor for v0.1.
Emergency stop: out-of-band control mechanism not dependent on model cooperation —
the agent must not be able to intercept its own emergency stop. Implement max task
duration, max steps, max retries, resource budget, and a stop signal outside model
reasoning. → **ADR-0016**

**Q17 — Scheduler.** In-process priority scheduler for v0.1. Priority: FOREGROUND USER
TASK > BACKGROUND LEARNING > BENCHMARK > BACKUP > MAINTENANCE. asyncio tasks + explicit
budgets. → **ADR-0017**

## D. Security

**Q18 — Windows sandbox.** Layered Windows restrictions rather than pretending perfect
sandbox isolation: restricted process mechanisms, Windows Job Objects, task-specific
workspaces, resource limits, network policy. Full VM/container-grade isolation is future
work. Make the Sandbox abstraction stronger than its first implementation
(filesystem policy, process limits, network policy, working directory, resource limits).
→ **ADR-0018**

**Q19 — Data classifier.** Deterministic-first: path heuristics, regex, file extensions,
known secret patterns, entropy, explicit user classification. Model-based classification
later. Uncertain → stricter classification. → **ADR-0019**

**Q20 — Audit integrity.** v0.1: append-oriented PostgreSQL audit table, local file
mirror where useful, optional hash-chain digest. Full tamper-evident infrastructure
later. Audit entries never controlled by the model. → **ADR-0020**

**Q21 — Secret store.** Windows Credential Manager / DPAPI behind the SecretManager
abstraction; encrypted local store fallback for portability. Never write plaintext
secrets into logs, audit, memory, prompts, experience records, or benchmark traces.
→ **ADR-0021**

## E. Repository / Development

**Q22 — Legacy code.** Do NOT delete immediately. Archive outside the active
architecture (`_legacy/`), exclude from normal agent indexing. The old code may contain
useful implementation patterns (Pydantic validation, fail-closed behavior, security
tests, benchmark structures). Do not import the old architecture into the new system.
→ **ADR-0022** (executed: legacy moved to `_legacy/`)

**Q23 — Python/toolchain.** Python 3.12; uv; ruff; mypy; pytest; pre-commit: yes.
Keep the toolchain boring and deterministic. → **ADR-0023**

**Q24 — CI.** Both local checks and GitHub Actions. Minimum CI: unit tests, integration
tests, security tests, static checks, type checks. Hardware-dependent tests explicitly
marked. → **ADR-0024**

**Q25 — License / packaging.** Do not finalize the open-source license purely as an
engineering assumption; perform dependency/license audit first. For Windows v0.1,
PowerShell bootstrap/setup is acceptable. → **ADR-0025**

## F. Smaller gaps

- **F1** Blueprint typo fixed: `textquality` → `quality` (BP §274). → **ADR-0026**
- **F2** CLI is the first user-facing control surface; web UI later. → **ADR-0026**
- **F3** pgvector allowed only as an experimental benchmark/reference backend.
  PostgreSQL remains the canonical structured database. → **ADR-0026**
- **F4** VisionModel capability abstraction; a multimodal LocalModel may implement it;
  do not maintain two unrelated model abstractions. → **ADR-0026**
- **F5** Add `session_id` to Task. Trace identity: user_id → session_id → task_id →
  run_id → step_id. Sessions are context containers, NOT memory boundaries. → **ADR-0026**
- **F6** Explicit precedence: SAFE_MODE forces stricter approvals; FULL_AUTONOMY still
  cannot override immutable security invariants. Security invariants > owner policy >
  task policy > agent/model preference. → **ADR-0026**
- **F7** Canonical 10-class failure taxonomy adopted; legacy classifications mapped in.
  → **ADR-0026**

## G. Session-independent memory — final decision

Mandatory. Sessions are NOT isolated memory stores. Sessions 1..N feed persistent
memory/experience (PostgreSQL, Vector Engine, Artifacts). Session 5 can retrieve
relevant information from any authorized prior session. Do not require replaying
previous conversations; memory is indexed independently from sessions. Scopes: SESSION,
TASK, PROJECT, USER, SYSTEM. Cross-session access is allowed for the same owner by
default, subject to project scope, sensitivity, permissions, current policy. A session
is a context boundary, not a knowledge boundary. (BP §376-420 unchanged.)

## H. Model selection — final decision

There is NO OmniRouter. NO Hermes-specific model architecture. NO mandatory single model.
The Agent Runtime uses ModelSelector: TASK → TASK ANALYSIS → REQUIRED CAPABILITIES →
MODEL REGISTRY → CAPABILITY FILTER → BENCHMARK HISTORY → REAL EXPERIENCE → HARDWARE →
RESOURCE REQUIREMENTS → POLICY → TASK CRITICALITY → MODEL SELECTION. Returns
primary_model, fallback_local_models, selection_score, selection_reason. No cloud
fallback. (BP §1.1, §7, §97, §188 unchanged.)

## I. Self-improvement — final decision

NOT immediate weight modification. Flow: EXECUTION → OBSERVE → EVALUATE → EXPERIENCE →
PATTERN DISCOVERY → PROPOSAL → SANDBOX → BENCHMARK → COMPARE → PROMOTE/REJECT → VERSION →
MONITOR → ROLLBACK. Primary improvement targets: model selection, workflow, planning
strategy, memory retrieval, tool usage, agent configuration. Model fine-tuning is
optional future work; if added: training/dataset generation/evaluation stay local,
deployment is versioned, rollback is mandatory. (BP §29-31, §65-67 unchanged.)

## J. Security principle — final decision

OPEN INTERNET + CLOSED PRIVATE-DATA BOUNDARY. The system may browse public websites and
fetch public information, but in v0.1 NO EXTERNAL LLM INFERENCE. No local files,
screenshots, memory, experiences, credentials, learning datasets, or private task
history may be sent to an external AI model. Every effectful action follows:
MODEL → AGENT RUNTIME → TOOL GATEWAY → SECURITY GATE → EXECUTION. (BP §200, §363-364,
§370 unchanged.)

## K. Final implementation order (authoritative)

```text
PHASE 0   Repository + tooling + interfaces
PHASE 1   PostgreSQL + core state
PHASE 2   Local model runtime + registry
PHASE 3   Security Gate + policies + permissions + audit
PHASE 4   Tool Gateway + filesystem + terminal
PHASE 5   Vision + Gemma adapter
PHASE 6   Computer control
PHASE 7   Browser + controlled Internet
PHASE 8   Memory Engine
PHASE 9   Native Vector Engine exact-search baseline
PHASE 10  Evaluation + verification
PHASE 11  Experience system
PHASE 12  Adaptive model selection
PHASE 13  Self-improvement + benchmarks
PHASE 14  Advanced vector engine / optimization
```

Do not skip the Security Gate to reach computer control faster. → **ADR-0027**

## L. Final strategic principle

Build the feedback loop: UNDERSTAND → PLAN → SELECT MODEL → OBSERVE → ACT → VERIFY →
EVALUATE → REMEMBER → LEARN → IMPROVE → BENCHMARK → VERSION → REPEAT — with the Security
Gate above the entire execution path. Models, tools, vector implementation, and UI are
replaceable. The stable core: USER AUTHORITY, SECURITY, LOCAL INFERENCE, VERIFIED
EXECUTION, PERSISTENT MEMORY, EXPERIENCE, SELF-IMPROVEMENT, REPRODUCIBILITY, ROLLBACK.
