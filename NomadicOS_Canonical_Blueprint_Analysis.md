# NomadicOS v0.1 Canonical Blueprint — Analysis & Strategic Report

> **RESOLVED 2026-09-04:** All 25 questions (Q1–Q25) and gaps (F1–F7) were answered and
> confirmed. See `docs/architecture/CANONICAL_ANSWERS_v0.1.md` (answers of record) and
> `docs/architecture/adr/README.md` (ADR-0001 … ADR-0027). Blueprint deltas and errata:
> `docs/architecture/BLUEPRINT_ADDENDUM_v0.1.md`. Legacy builds archived to `_legacy/`
> per Q22/ADR-0022. Section 1 below is retained as the historical question list.

> **Source of truth analyzed:** `NomadicOS_v0.1_Canonical_Blueprint.md` (9,095 lines, 420 sections)
> **Analysis date:** 2026-09-04
> **Status:** Analysis deliverable — **no implementation performed** (per instruction: nothing proceeds until questions are answered and the framework is confirmed)
> **Scope of this report:** (1) Gap analysis & clarifying questions, (2) Strategic roadmap, (3) Development environment configuration for Kilocode/Antigravity, (4) Interactive architectural map design

---

## 0. Executive Summary of the Blueprint

The canonical blueprint redefines NomadicOS as a **local-first autonomous AI operating environment** — a computer operator, not a chat pipeline. The five pillars:

1. **Local-only inference** — no external LLM calls exist in v0.1 (§1.3, §200, §286). The Internet is an *information source*, never an *AI brain* (§0).
2. **Mediated authority** — models propose; the Security Gate authorizes (§3.3, §73, §363).
3. **Evidence over claims** — closed-loop OBSERVE→ACT→VERIFY, deterministic evaluation, no self-reported success (§3.4, §5, §28, §366).
4. **Evidence-driven self-improvement** — sandbox → benchmark → compare → promote/reject/rollback; never "model says it's better" (§3.5, §29-31, §365).
5. **Persistent cross-session memory** — sessions are context boundaries, not memory boundaries (§376-420).

### 0.1 Deltas vs. previous iterations (now superseded)

| Previous iteration | Canonical v0.1 | Blueprint refs |
|---|---|---|
| OmniRouter provider layer | **Removed.** `ModelSelector` inside Agent Runtime | §1.1, §1.5, §8.4 |
| External LLM inference (GLM via provider) | **Removed.** Local models only | §1.1, §1.3, §370 |
| No database ("no PostgreSQL yet") | **PostgreSQL is canonical** for all structured state | §1.2, §19 |
| Skill/tool execution planned ad hoc | Tool Gateway + Security Gate mandatory mediation | §11-13, §36, §98 |
| Session-scoped chat memory | Persistent cross-session memory engine | §376-420 |
| Cloud vector DB possibilities | Native vector engine (own build), external backends only experimental | §20-22, §213 |
| Planner as standalone LLM layer | Planning absorbed into Agent Runtime | §6.1, §7 |
| Cloud model fallback chains | Local-only fallback; no cloud fallback | §188, §149 |

**Existing repository code (`input/`, `models/`, `planner/`, `main.py`) is architecturally invalid under the canonical blueprint** (it is built around external providers and OmniRouter). Disposition is raised as Question Q22 — nothing was deleted or modified.

---

## 1. Gap Analysis & Clarifying Questions

Questions are grouped by subsystem and numbered for reference. Each is phrased so it can be answered directly ("Answer: B") and converted into an ADR (Architecture Decision Record).

### A. Local Model Runtime & Models

**Q1 — Which local inference runtime should the `LocalModel` adapter target first (§8.1, §301 "pluggable local inference runtime")?**
Options: (a) Ollama (HTTP, manages VRAM/load/unload for us), (b) llama-cpp-python (in-process GGUF, fine control), (c) LM Studio local server, (d) vLLM (Linux-oriented), (e) decide via spike in Phase 2. This decision determines the Windows-first path (§173), VRAM management (§53, §156-157), and structured-output support (§86).

**Q2 — What is the minimum hardware profile v0.1 must support (§170)?**
VRAM/RAM floor, GPU vendor (NVIDIA/CUDA vs AMD vs CPU-only fallback). This constrains model choices, quantization, and whether a 7B-class model is the planning baseline.

**Q3 — Which local embedding model is canonical for the vector engine (§345-347)?**
Dimensions + metric must be fixed early because they are baked into the native vector index format (§311, §107). Candidate shapes: bge-small/gte-small (384d), nomic-embed (768d), all-MiniLM (384d) — all runnable locally.

**Q4 — Vision: which exact Gemma-family variant and serving path (§1.4, §10, §62)?**
Gemma-3 multimodal GGUF via the chosen runtime, or a separate vision serving process? Must clarify whether vision goes through the same `LocalModel.generate_with_images(...)` interface or a dedicated `VisionModel` interface (§8.1 vs §209 lists `VisionModel` separately — slight contradiction).

**Q5 — Model acquisition: which source and what integrity standard (§150-151)?**
HuggingFace direct? Is checksum verification against publisher manifests mandatory before registration, and is there an allowlist of model publishers in v0.1?

**Q6 — Model switching granularity (§8.5, §187): is concurrent multi-model residency required in v0.1, or sequential load/unload?**
The blueprint shows planning + reasoning + vision + fast models in one task (§8.5) but also resource pressure/unloading (§153, §157). On a single consumer GPU these conflict; the milestone-1 expectation should be pinned (likely: sequential with one resident model, swap on demand).

### B. Data & Storage

**Q7 — Native Vector Engine V0 (Phase 10): pure-Python exact search, or start from pgvector?**
§20-21 mandates a native engine with exact search first; §20.2 mentions experimental external backends. Confirm that v0.1 interim = pure-Python/NumPy exact search over a local file store, with pgvector/Qdrant only as *experimental benchmark backends* (§213-214) — not the production path.

**Q8 — Where does vector data live on disk, and what file format for V0 (§21, §106, §311)?**
Confirm layout under `NOMADICOS_DATA_DIR` (§103), e.g. `data/vector/<index-id>/` with JSON/NumPy snapshots + `format_version` header, before WAL (§308).

**Q9 — PostgreSQL deployment on Windows-first (§173, §76):**
(a) Docker via `docker-compose.dev.yml`, (b) native Windows service, (c) both with a setup wizard (§130). Also: which migration tool (Alembic vs plain versioned SQL files under `postgres/migrations/` §76)?

**Q10 — Database access layer: asyncpg vs psycopg3 (§93, §250)?**
Blueprint mandates domain APIs and transactions; the driver choice sets the async model for the whole runtime.

**Q11 — Backup tooling for v0.1 (§43, §105):**
`pg_dump`-based snapshots managed by BackupManager, or filesystem-level volume snapshots? And where are encrypted backups stored locally?

### C. Architecture & Process Model

**Q12 — Process model: single Python process (asyncio) with in-proc subsystems, or multi-process with IPC (§91, §129)?**
§91/§129 describe supervised child processes and authenticated IPC; §247-249 describe a scheduler and conservative concurrency. Confirm v0.1 = single process + background asyncio jobs, with process supervision deferred until a concrete need (e.g., long tasks) — or the opposite.

**Q13 — Constitution representation (§4, §257, §190):**
Confirm the Constitution is implemented as (a) an immutable-invariants module in code (invariant test suite = executable constitution, §191) plus (b) versioned YAML policy files for user-configurable rules, with precedence per §262. Who may edit owner policies — UI only, or files + CLI in v0.1?

**Q14 — Internal API & UI form for v0.1 (§54-55, §127-128, §375):**
Milestone order puts UI last. Confirm v0.1 control surface = (a) CLI + optional local web dashboard (FastAPI + static pages, localhost-authenticated), (b) TUI first, or (c) defer UI entirely to a later phase with audit/log files as the observability surface. Also: does an HTTP API exist at all in v0.1, or purely in-process?

**Q15 — Task intake channel for Milestone 1 (§78):**
Before UI exists, how does the user submit the "Open VS Code, inspect project X, run tests..." task — a CLI command (`nomadicos task create ...`)? Confirm CLI-first intake.

**Q16 — Watchdog & emergency stop implementation on Windows (§70, §122, §121):**
Confirm: watchdog = supervisor thread/task inside the runtime (not a separate OS service) for v0.1, and emergency stop = out-of-band control signal (CLI/console event) that the agent loop cannot intercept.

**Q17 — Scheduler/job classes for v0.1 (§244-249):**
Confirm in-process priority scheduler (foreground user tasks > learning/benchmarks/backups) with asyncio-based budgets, rather than OS-level cron/task-scheduler integration.

### D. Security

**Q18 — Sandbox reality on Windows (§35, §91):**
Windows lacks cheap per-process universal sandboxing. Confirm v0.1 execution levels map to: TRUSTED_LOCAL (in-proc), RESTRICTED (restricted token / Job Objects with limits), SANDBOXED (job object + temp workspace + no-network via policy), HIGH_RISK_APPROVAL (user gate) — and that OS-level VM/container isolation is future work.

**Q19 — Data Classifier v0.1 scope (§264-266):**
Confirm deterministic-first: regex/pattern + path heuristics + extension/entropy rules, with model-based classification as a later local-model feature. Classification is conservative (uncertain ⇒ stricter, §266).

**Q20 — Audit integrity level for v0.1 (§41-42, §251):**
Confirm append-only PostgreSQL audit table + file mirror + optional daily hash-chain digest; full tamper-evident hash chaining deferred.

**Q21 — Secret store implementation (§40, §103):**
Confirm Windows Credential Manager / DPAPI-backed storage behind the `SecretManager` abstraction, with file-based encrypted fallback for portability.

### E. Repository, Tooling & Process

**Q22 — Disposition of the legacy builds (`input/`, `models/`, `planner/`, `main.py`):**
They contradict the canonical blueprint (external providers, OmniRouter). Options: (a) archive to `_legacy/` (excluded from indexing), (b) delete outright, (c) keep as read-only reference docs. Note: their *patterns* (fail-closed validators, strict Pydantic boundary models, security-test style) are worth porting into the new architecture as reference material.

**Q23 — Python version and toolchain (§76, §301):**
Confirm Python 3.12+ (or 3.13), package manager (uv / pip-tools / poetry), lint/format (ruff), type checking (mypy/pyright), and pre-commit. Blueprint mandates typed interfaces and structured logging (§83).

**Q24 — CI platform (§294 "security tests are part of CI"):**
Local-only CI (pre-commit + scripts) vs GitHub Actions vs both? Blueprint is local-first but the repo may still be hosted remotely.

**Q25 — License & packaging target for v0.1 (§76, §130):**
OSS license choice, and whether "installer/setup script" means a PowerShell bootstrap + pip install for v0.1.

### F. Smaller specification gaps (for the record; low blocking risk)

- **F1** — §274 contains a formatting typo (`textquality` → `quality`).
- **F2** — §55/§54 UI concepts vs Phase order: UI appears in §375 last; confirm Milestone 1 (§78) report surfaces via CLI only.
- **F3** — §20.1 research targets include pgvector while §1.2 excludes cloud vector DBs — pgvector is local Postgres; confirm it's permitted as an experimental backend only (aligns with §20.2 wording).
- **F4** — Vision interface naming: §8.1 embeds vision in `LocalModel.generate_with_images`; §209 lists a separate `VisionModel` interface. Recommend: `VisionModel` capability interface that a multimodal `LocalModel` also satisfies.
- **F5** — Session object (§378) vs Task object (§6.3): the Task lacks `session_id`; recommend adding it so §399's `user_id/session_id/task_id/run_id` correlation is structurally enforced.
- **F6** — §132 startup modes vs §39 approval modes: interaction matrix (e.g., SAFE_MODE forces approval regardless of FULL_AUTONOMY) should be made explicit.
- **F7** — Failure taxonomy (§117) vs previous `FailureClass` vocabularies: confirm the canonical 10-class list replaces all prior taxonomies.

---

## 2. Strategic Roadmap

### 2.1 Recommended sequencing (aligned with §77/§375, with optimizations)

```text
Phase 0   Repo & tooling            → pyproject, uv, ruff, mypy, pytest, pre-commit, docker-compose (PG),
                                      config loader (pydantic-settings), structured logging, error taxonomy module,
                                      ADR template, .kilocodeignore/AGENTS.md (see §3 of this report)
Phase 0.5 Interface skeletons       → All §209 interfaces as ABCs + Pydantic contracts + fake adapters
                                      (FakeLocalModel, FakeVisionModel, FakeVectorStore, FakeSecretStore)
                                      so every later phase is testable without hardware/models
Phase 1   PostgreSQL & core state   → schemas core/tasks/agents/models/audit/config (§19), repository layer (§93),
                                      migration runner, health checks
Phase 2   Local model runtime       → LocalModel ABC + chosen runtime adapter + registry (PostgreSQL) +
                                      benchmark stub; FAKE model adapter for CI (no GPU in tests)
Phase 3   Security Gate FIRST-RUN   → Build the Security Gate + permissions + policy engine + audit BEFORE
                                      broad tool execution (§375 explicitly forbids skipping this)
Phase 4   Tool Gateway + FS/Terminal→ schema-validated tool calls (§86), risk levels (§38), budgets (§72),
                                      dry-run (§139), evidence-bearing results (§143)
Phase 5+  Vision → Computer control → Network → Memory → Vector V0 → Evaluation → Experience → Learning
```

**Deviation from §77 (documented for ADR):** pulling the Security Gate to Phase 3 (before tooling) instead of Phase 4. §375's own start command (`PostgreSQL → Core → Security → Local Model Runtime → …`) supports this; §77's phase list is used as the implementation-content checklist.

### 2.2 Architectural optimizations & strategic suggestions

1. **The Constitution as an executable artifact.** Implement §4.2's fifteen invariants as an `invariants.py` module + a dedicated pytest suite (§191, §283-294) that runs in CI and *at startup* (§131). The test suite literally becomes the Constitution's enforcement mechanism, not just documentation.
2. **Correlation-ID spine via a tiny internal event bus.** §254-255 requires `request_id/task_id/run_id/step_id` traceability across model/tool/network/audit calls. A minimal typed event bus (async, in-proc) with a mandatory `TraceContext` header on every event makes audit, experience recording, and the future UI trivially consistent — and prevents retrofitting correlation later.
3. **Repository pattern over PostgreSQL (§93).** Domain APIs (`task.get`, `memory.store`, …) with zero raw SQL outside `postgres/` keeps §93's "no arbitrary SQL from agents" guarantee structural rather than disciplinary.
4. **Fake-first testing strategy (extends §47-style practice).** Every hardware-dependent subsystem (model runtime, vision, screen capture, clipboard) gets a fake adapter in Phase 0.5. This lets the Agent Runtime, Security Gate, Memory Engine, and even the native vector engine be fully developed and CI-tested on any machine, with hardware integration tests gated behind an explicit marker (`@pytest.mark.hardware`).
5. **Native vector engine discipline.** Scope-lock V0 to §21's eight operations + exact search over NumPy arrays with JSON-snapshot persistence and `format_version` headers. Resist ANN until Recall@K measurement against the exact baseline exists (§304-305). This is the highest scope-creep risk in the blueprint; the benchmark matrix (§314) is the brake.
6. **Capability-based model & vision unification.** Resolve F4 by making `VisionModel` a capability protocol; a multimodal `LocalModel` satisfies both. The registry (§8.2) stores `vision_capable` and the selector treats vision as a required-capability filter, exactly like tool_use.
7. **Policy-as-code with schema + simulation (§257-259).** Versioned YAML policies validated by Pydantic at load, fail-closed on invalid (consistent with §85), plus a `policy.simulate` dry-run path before activation — this mirrors the blueprint's own dry-run philosophy (§139).
8. **ADR workflow (§372.9).** Every answer to Section 1's questions becomes a short ADR (`docs/architecture/adr/NNNN-*.md`) recording decision, rationale, and blueprint-section references. This satisfies §298's change policy and keeps the blueprint authoritative while allowing documented deviation.
9. **Salvage list from legacy code (pending Q22).** Worth porting as *patterns*, not code: strict Pydantic boundary models with `extra="forbid"`/`frozen`, evidence-bearing tool results, security-test style (static sink scans, injection fixtures), benchmark traceability tables.
10. **Risk register (top 5):**
    - *Vision runtime availability on Windows* → mitigate with Q1 spike + accessibility-first grounding (§174-177), vision as fallback.
    - *Native vector engine scope creep* → V0 scope lock + benchmark gate (§213).
    - *Single-GPU model thrash* → Q6 sequential residency decision + preload policies (§156-157).
    - *Security Gate bypass via convenience* → invariant test suite + code-review checklist (§299) enforced in PRs.
    - *OneDrive-hosted working directory* → see §3.4 of this report (data integrity + indexing performance risk).

### 2.3 Definition-of-Done traceability

§81's twenty-item v0.1 checklist and §300's minimum acceptance checklist map 1:1 onto Phases 1-12. Recommend converting both into `docs/development/definition-of-done.md` as live checkboxes with phase tags, reviewed at each phase gate.

---

## 3. Development Environment Configuration — Kilocode × Antigravity

Goal: **maximum signal, minimum tokens** — the 9,095-line blueprint must never need to be re-read wholesale by the coding agent; it should be referenced by section ID.

### 3.1 Repository relocation (critical, do first)

The current working directory lives inside **OneDrive sync** (`C:\Users\dhruv\OneDrive\Desktop\NomadicOS`). For this project that is a concrete hazard: PostgreSQL data directories, vector index files, WAL segments, and model weights under active sync cause file-watcher churn, partial-sync corruption risk, and index rebuilds in both Kilocode and Antigravity.

**Recommendation:** move the working repo to a non-synced path (e.g., `C:\dev\nomadicos`), keep only `docs/` mirrored to OneDrive if desired, or at minimum mark the data directories OneDrive-excluded ("Always keep on this device" + exclusion). This is the single highest-leverage environment change.

### 3.2 Files to create at repo root (Phase 0, no implementation conflict)

**`.gitignore` / sync exclusions**
```gitignore
__pycache__/
*.py[cod]
.venv/
.env
.pytest_cache/
.ruff_cache/
.mypy_cache/
postgres-data/
vector-data/
data/
logs/
artifacts/
models/**/*.gguf
models/**/*.safetensors
models/**/*.bin
*.sqlite
.quarantine/
downloads/
screenshots/
```

**`.kilocodeignore`** (keeps Kilocode's context/indexing lean — these files are never pulled into the agent's context)
```gitignore
# Heavy/generated/secret — never index
_legacy/
postgres-data/
vector-data/
data/
logs/
artifacts/
models/
downloads/
.quarantine/
screenshots/
.venv/
**/__pycache__/
*.gguf
*.safetensors
*.bin
*.sqlite
*.db
.env

# Keep blueprint + architecture docs indexed (they are the spec), but
# exclude per-phase scratch material
docs/scratch/
```

**`AGENTS.md`** (the always-loaded context contract — replaces re-reading the blueprint)
Contents plan:
1. **Canonical spec pointer:** `NomadicOS_v0.1_Canonical_Blueprint.md` is the single source of truth; cite sections as `BP §NNN`; never restate it.
2. **Non-negotiables digest:** local-only inference (§1.3, §200), no OmniRouter/Skill System/Hermes (§1.1, §370), PostgreSQL canonical (§1.2), Security Gate mediation (§73, §363), fail-closed (§85), evidence over claims (§366), sessions ≠ memory boundaries (§377).
3. **Command block:** `uv sync`, `docker compose -f docker-compose.dev.yml up -d`, `pytest -m "not hardware"`, `ruff check .`, `mypy src/`.
4. **Layout map:** `src/nomadicos/<subsystem>/` one-liner per subsystem (§76).
5. **Current phase banner:** "We are in Phase N — only touch subsystems X/Y" (updated per session).
6. **Error taxonomy & logging rules:** §84 exceptions, §134/§256 redaction rules.
7. **PR checklist:** §299 verbatim.

**`.kilo/rules/security-invariants.md`** (always-on rule file)
The 15 invariants (§4.2) + "never reintroduce §1.1 components" + fail-closed default — phrased as rules the agent must verify before proposing code.

**`.kilo/rules/current-phase.md`** — updated at each phase transition so every session starts with correct scope without re-reading the blueprint.

### 3.3 Indexing strategy (cost-efficient)

1. **Index surface = current phase only.** Early phases touch `src/nomadicos/{core,postgres,constitution,security}` — the legacy folders, `ui/`, `vector/`, `learning/` stay unindexed (and, per Q22, possibly archived) until their phase starts. Antigravity's workspace indexing and Kilocode's context both scale with the file tree; a Phase-1 repo is ~30 files, not hundreds.
2. **Blueprint by reference, not by inclusion.** `AGENTS.md` carries a section-index map (e.g., "Security Gate → BP §36, §85, §98, §191, §283-294"). The agent reads the specific 100-line section on demand (`@file` mention) instead of carrying 12k tokens permanently.
3. **Docs indexing policy:** keep `docs/architecture/`, `docs/security/`, ADRs, and the blueprint indexed (they are the spec and small); exclude everything else generated.
4. **Memory Bank / session notes:** maintain a rolling `docs/development/session-notes.md` (decisions made, tests added, next step) so new sessions restore context in ~500 tokens instead of re-deriving it.
5. **Antigravity specifics:** keep the workspace root at the repo root (not a parent folder), exclude the sync-heavy OneDrive path (§3.1), and let Antigravity's local index warm on the small Phase-0 tree; avoid committing model weights or DB data anywhere inside the workspace.
6. **Cost discipline in prompts:** forbid pasting blueprint excerpts or generated artifacts (migrations, lockfiles) into chat; require file paths + section IDs instead. Structured logs (`§134`) are the debugging surface, not raw dumps.

### 3.4 Environment checklist (ordered)

1. [ ] Relocate repo out of OneDrive (or exclude data dirs) — §3.1
2. [ ] Python 3.12+ via uv; `pyproject.toml` with dev extras (pytest, ruff, mypy, pre-commit) — Q23
3. [ ] `docker-compose.dev.yml` for PostgreSQL 16+ with `postgres-data/` volume ignored — Q9
4. [ ] `.gitignore` + `.kilocodeignore` + `AGENTS.md` + `.kilo/rules/*` as above
5. [ ] `.env.example` with §103 variables (`NOMADICOS_*`, `POSTGRES_*`)
6. [ ] pytest markers: `hardware`, `security`, `slow`; security suite wired to fail CI — §294
7. [ ] ADR directory + template; analysis-of-record = this document

---

## 4. Interactive Architectural Map — Conceptual Design

A long-term, always-current documentation artifact (satisfies §295-297) that renders the canonical component graph (§75/§373) as an interactive diagram where **clicking any component reveals what it is, how it is built, and why**.

### 4.1 Design goals

1. Single offline HTML file (local-first principle; no CDN, works in OFFLINE mode §57).
2. Generated from a machine-readable manifest + repo scan so it cannot drift from the code (§298).
3. Serves owner onboarding, security review, and LLM context (the manifest doubles as structured documentation).

### 4.2 Data model — `docs/architecture/architecture.json`

Each component is a node; edges are data/control flows; each node carries implementation + rationale metadata:

```json
{
  "id": "security-gate",
  "title": "Security Gate",
  "layer": "security",
  "blueprint_refs": ["§36", "§73", "§85", "§98", "§191", "§283-294"],
  "status": "planned | in_progress | built",
  "phase": 3,
  "summary": "Mediates every effectful action: ALLOW / ASK / BLOCK.",
  "responsibilities": [
    "Evaluate agent/tool/target/sensitivity/risk (§36.1)",
    "Fail closed on unknown policy (§85)"
  ],
  "interfaces": ["security.authorize(action) -> Decision"],
  "technologies": [
    {"name": "Pydantic policy schemas", "why": "typed, versioned, machine-enforced (§257)"},
    {"name": "PostgreSQL audit tables", "why": "append-only, queryable (§41)"},
    {"name": "Windows Job Objects (via pywin32)", "why": "process-level limits for RESTRICTED/SANDBOXED (§35, Q18)"}
  ],
  "rationale": "Models are untrusted proposers; a single mandatory mediation point is the only place authorization can be non-bypassable (§73, §363).",
  "invariants_guarded": ["I3 no privilege escalation", "I4 no policy modification", "I5 no gate bypass"],
  "depends_on": ["policy-engine", "permission-engine", "audit-manager", "data-classifier"],
  "used_by": ["tool-gateway", "network-gateway"],
  "security_notes": ["Fail-closed default", "Immutable core policy separate from learned policy (§190)"],
  "tests": ["tests/security/test_gate_*.py"]
}
```

Edges:
```json
{ "from": "agent-runtime", "to": "tool-gateway", "kind": "control", "label": "mediated tool request" },
{ "from": "tool-gateway", "to": "security-gate", "kind": "trust", "label": "authorization (§73)" }
```

### 4.3 Visualization

- **Renderer:** static `architecture.html` — inline SVG node-link graph of the §75/§373 layout, plain JavaScript + a small pan/zoom helper, all inlined (zero network dependencies). Data injected as a JSON blob at generation time.
- **Default view:** the canonical graph with layers color-coded (Control / Agent / Intelligence / Memory / Security / Execution / Learning / Interface).
- **Interactions:**
  - **Click node** → side panel: summary, responsibilities, interfaces, technologies (+ why), rationale, guarded invariants, blueprint refs (clickable anchors into the blueprint MD), implementation status, test list.
  - **Hover edge** → data-flow description; **trust edges** (model→gateway→gate→system) rendered distinctively so the §73 boundary is always visible.
  - **Filters:** by layer, by phase (Milestone timeline slider — shows what exists at each §77 phase), by status (planned/in_progress/built).
  - **Overlays (toggles):** *Trust boundary* (highlights the §73 chain and Constitution precedence §262), *Data locality* (highlights everything that must never cross to the Network Gateway — §16, §199), *Session/memory lens* (renders the §376-420 persistent-memory view: sessions as translucent contexts over persistent stores), *Cross-cutting* (audit/backup/policy rays).
  - **Search:** component/technology/invariant lookup.
  - **Detail deep-links:** each node links to its ADRs, README, and test suite — the map is the index of the documentation system.

### 4.4 Generation & maintenance pipeline

```text
docs/architecture/architecture.json   (hand-maintained manifest; PR-reviewed)
        +
repo scan (script)                    (verify files/tests/ADRs referenced by nodes actually exist;
                                       warn on drift → CI check)
        ↓
scripts/generate_arch_map.py
        ↓
docs/architecture/architecture.html  (committed, openable offline, shareable as a single file)
```

- **CI check:** a test asserts every `built` node's declared files/tests exist, and every subsystem README (§295) links back to its node — the map fails CI when documentation drifts, mirroring §294's philosophy for docs.
- **Update ritual:** any architectural change PR must touch `architecture.json` (part of the §298/§299 checklist).

### 4.5 Why this design

- **Offline single file** matches the product's own philosophy (§57, local-first) and makes it trivially shareable (one HTML file + the JSON).
- **Manifest-driven** keeps humans and LLM agents honest: the same JSON is consumable by Kilocode as lightweight context (a 200-line index instead of re-reading the blueprint), multiplying the cost savings in §3.
- **Rationale-first nodes** directly implement the request: selecting a component reveals *implementation details, specific technologies, and the reasoning for their selection* — and preserves that knowledge as the project evolves.

---

## 5. Next Step

Per the working agreement: **no implementation has been started and no existing code has been modified or deleted.** The blueprint's Phase 0 begins only after the questions in Section 1 — minimally Q1, Q6, Q7, Q9, Q12, Q14, Q22 — are answered and the architectural framework is confirmed. Each answer will be recorded as an ADR, and the recommendations in Section 2 can be amended before the first commit.
