import { useState, useEffect, useMemo, useRef, useCallback, memo } from "react";
import * as d3 from "d3";
import {
  Search, X, ZoomIn, ZoomOut, Maximize2, Play, Pause,
  SkipBack, SkipForward, ShieldCheck, ShieldAlert, Waypoints, Workflow,
  CheckCircle2, CircleDashed, FileCode2, FlaskConical, Link2, ArrowRight,
  Terminal, Cpu, Bot, BrainCircuit, Eye, Globe, Database,
  HardDrive, Wrench, RefreshCw, Copy, Check, Boxes,
} from "lucide-react";
// Architecture data, embedded so this file works standalone.
// Edit this object (or paste a regenerated export over it) to update
// the explorer -- every view, filter, and layout derives from it.
const ARCHITECTURE_DATA = {
  "meta": {
    "project": "NomadicOS v0.1",
    "subtitle": "Local-first autonomous AI operating environment",
    "tagline": "Your machine. Your models. Your data.",
    "generated": "2026-09-07",
    "blueprint": "NomadicOS_v0.1_Canonical_Blueprint.md",
    "stats": {
      "modules": 91,
      "tests": 274,
      "adrs": 31,
      "phases": 14
    }
  },
  "layers": [
    {
      "id": "interface",
      "name": "Interface",
      "color": "#7dd3fc"
    },
    {
      "id": "runtime",
      "name": "Runtime Core",
      "color": "#a5b4fc"
    },
    {
      "id": "agent",
      "name": "Agent Runtime",
      "color": "#c4b5fd"
    },
    {
      "id": "intelligence",
      "name": "Model Intelligence",
      "color": "#f0abfc"
    },
    {
      "id": "security",
      "name": "Security",
      "color": "#fda4af"
    },
    {
      "id": "tools",
      "name": "Tool Execution",
      "color": "#fdba74"
    },
    {
      "id": "perception",
      "name": "Perception & Action",
      "color": "#fcd34d"
    },
    {
      "id": "network",
      "name": "Network",
      "color": "#86efac"
    },
    {
      "id": "knowledge",
      "name": "Knowledge & Learning",
      "color": "#67e8f9"
    },
    {
      "id": "data",
      "name": "Data",
      "color": "#94a3b8"
    }
  ],
  "nodes": [
    {
      "id": "api",
      "title": "Local API (FastAPI)",
      "layer": "interface",
      "x": 70,
      "y": 900,
      "status": "built",
      "bp": ["ADR-0032"],
      "code": ["src/nomadicos/api/"],
      "summary": "Local service layer: 6 endpoints, bearer token auth, SSE streaming - the access path for terminals, scripts, and future dashboards (ADR-0032).",
      "what": [
        "POST /v1/goals (idempotency-key safe)",
        "GET /v1/goals/{id} + GET /v1/events/{id} (SSE)",
        "POST /v1/memory/search, GET /v1/status, POST /v1/emergency-stop"
      ],
      "how": [
        "Auth: bearer token from data/api-token, fail closed on every endpoint",
        "Goals run as background asyncio tasks with an in-memory registry",
        "Token is never logged (I12); the gate still mediates every goal (I5)"
      ],
      "tech": [
        {
          "name": "FastAPI + uvicorn",
          "why": "async-native HTTP wrapper over the asyncio Runtime loop"
        }
      ],
      "why": "ADR-0032: NomadicOS reachable from any terminal, script, or future dashboard without modifying the Python core.",
      "invariants": ["I5 no gate bypass", "I12 token containment"],
      "tests": ["tests/unit/api/"]
    },
    {
      "id": "cli",
      "title": "CLI Intake",
      "layer": "interface",
      "x": 70,
      "y": 120,
      "status": "built",
      "bp": [
        "§78",
        "§121",
        "ADR-0015"
      ],
      "code": [
        "src/nomadicos/cli.py"
      ],
      "summary": "The owner's control surface: create tasks, check status, pull the emergency stop.",
      "what": [
        "nomadicos task create \"<goal>\" — submits a goal into the Agent Runtime loop",
        "nomadicos status — one-line subsystem health (persistence, tools, models, stop state)",
        "nomadicos stop — out-of-band emergency stop the model can never intercept"
      ],
      "how": [
        "argparse subcommands; task create calls Runtime.run_goal() directly",
        "Same in-process interface the future web UI will use — no second control path (ADR-0014)",
        "Report renders requested vs completed vs verified vs failed (BP §180-181)"
      ],
      "tech": [
        {
          "name": "argparse",
          "why": "stdlib; deterministic intake with zero dependencies"
        },
        {
          "name": "asyncio.run",
          "why": "bridges sync CLI into the async runtime"
        }
      ],
      "why": "ADR-0015: CLI-first was chosen so the system is testable and automatable before any UI exists; the UI later becomes just another client of the same interface.",
      "invariants": [
        "I15 user authority",
        "BP §122 emergency stop"
      ],
      "tests": [
        "tests/integration/test_agent_runtime.py"
      ]
    },
    {
      "id": "runtime",
      "title": "Runtime Composition Root",
      "layer": "runtime",
      "x": 70,
      "y": 300,
      "bp": [
        "§234",
        "§237",
        "ADR-0009"
      ],
      "code": [
        "src/nomadicos/core/runtime.py"
      ],
      "summary": "Wires every subsystem together and owns the persistence posture.",
      "what": [
        "Loads owner policies (fail-closed if invalid), connects PostgreSQL, runs migrations",
        "Registers the model fleet from Ollama discovery, demotes CI fakes when real models appear",
        "Registers tools (filesystem, terminal, web) and enables the Network Gateway"
      ],
      "how": [
        "PostgreSQL canonical: if reachable, migrations run and Postgres audit/memory/experience stores activate; if not, the runtime degrades to in-memory stores (BP §237) — degraded, never permissive",
        "Fleet watch loop re-syncs the model registry every 5 minutes (new/removed/re-pulled models)"
      ],
      "tech": [
        {
          "name": "psycopg 3",
          "why": "ADR-0010: modern async-capable PostgreSQL driver (LGPL satisfied by pip use)"
        },
        {
          "name": "Migration runner",
          "why": "versioned SQL files with checksum tamper detection (BP §104)"
        }
      ],
      "why": "BP §237 demands the system never crash on missing infrastructure — it degrades and reports. PostgreSQL is canonical state (BP §1.2); agents never see raw SQL (BP §93).",
      "invariants": [
        "I6 fail closed",
        "I7 audit never model-controlled"
      ],
      "tests": [
        "tests/unit/core/",
        "tests/integration/test_phase1_postgres.py"
      ]
    },
    {
      "id": "lifecycle",
      "title": "Lifecycle & Task State Machine",
      "layer": "runtime",
      "x": 70,
      "y": 470,
      "bp": [
        "§130",
        "§132",
        "§137",
        "§234-235"
      ],
      "code": [
        "src/nomadicos/core/lifecycle.py"
      ],
      "summary": "Startup/shutdown sequences plus the explicit task state machine (PLANNED → RUNNING → VERIFYING → SUCCESS/FAILED).",
      "what": [
        "Ordered subsystem startup; security initializes before autonomous execution (BP §234)",
        "Legal task transitions only — illegal jumps raise StateTransitionError",
        "Emergency stop latch: settable only from the owner surface (CLI), unreadable to the model"
      ],
      "how": [
        "Transition table enforces the DAG of states at runtime",
        "Startup emits lifecycle events on the event bus for audit correlation"
      ],
      "tech": [
        {
          "name": "Pydantic state models",
          "why": "persisted task state survives restarts (BP §136)"
        }
      ],
      "why": "Free-form model text can never drive system state (BP §86, §137) — only the explicit machine can.",
      "invariants": [
        "I6",
        "I10",
        "BP §122 emergency stop"
      ],
      "tests": [
        "tests/integration/test_phase0_core.py"
      ]
    },
    {
      "id": "agent-loop",
      "title": "Agent Runtime Loop",
      "layer": "agent",
      "x": 300,
      "y": 150,
      "bp": [
        "§50",
        "§185",
        "§78"
      ],
      "code": [
        "src/nomadicos/agent/runtime.py"
      ],
      "summary": "The canonical observe → propose → mediate → execute → verify loop until the goal is met or a budget stops it.",
      "what": [
        "Asks the model for ONE structured JSON tool call per step (never raw commands)",
        "Mediates every proposal through schema validation → budget → Security Gate → gateway",
        "Verifies outcomes with evidence, records experience, emits the truthful report"
      ],
      "how": [
        "Tool schemas are surfaced in the proposal prompt so the model emits compliant arguments (BP §141)",
        "Loop exits on finished=true, budget exhaustion, or an unrecoverable error — status never lies (BP §181)",
        "Unknown tools raise PermissionDenied; failures become evidence for the next attempt (BP §241)"
      ],
      "tech": [
        {
          "name": "json proposal parsing",
          "why": "BP §86: structured calls only, raw text is never executed"
        },
        {
          "name": "TaskBudgetTracker",
          "why": "I10: budgets enforced outside the model (BP §72)"
        }
      ],
      "why": "BP §185 fixes this exact loop; verification separates 'tool returned 0' from 'goal achieved' (BP §28, §366).",
      "invariants": [
        "I5 gate bypass",
        "I8 no evidence fabrication",
        "I10 bounded execution"
      ],
      "tests": [
        "tests/integration/test_agent_runtime.py"
      ]
    },
    {
      "id": "orchestrator",
      "title": "Multi-Agent Orchestrator",
      "layer": "agent",
      "x": 300,
      "y": 610,
      "status": "built",
      "bp": [
        "§364",
        "§244-249",
        "§375",
        "ADR-0030"
      ],
      "code": [
        "src/nomadicos/agent/orchestrator.py",
        "src/nomadicos/agent/roles.py"
      ],
      "summary": "Decomposes a goal into a bounded subtask DAG (planner), runs independent subtasks concurrently in waves (workers), merges results (synthesizer) — every agent still behind the same Security Gate (ADR-0030).",
      "what": [
        "Planner decomposes the goal into ≤ 6 subtasks; the DAG is acyclic by construction — invalid plans degrade to single-agent mode (ADR-0030)",
        "Wave execution: dependency-satisfied subtasks run as concurrent worker agents; the next step consumes all outputs of the finished wave (BP §244-249)",
        "Per-agent attribution: every agent carries an agent_id into Security Gate decisions, budgets, and audit records (BP §364)"
      ],
      "how": [
        "Roles share one AgentRuntime loop with different identity + directive + tools (planner / worker / synthesizer — roles.py)",
        "Workers never call each other — all tool execution passes the same gate; no multi-agent shortcut around mediation (I5, BP §375)",
        "A failed subtask cascades to dependents (failed/skipped); the final report stays truthful — PARTIAL/FAILED (BP §181)"
      ],
      "tech": [
        {
          "name": "asyncio waves",
          "why": "concurrent workers queue on model residency; Ollama serves concurrent requests per model (ADR-0006 Option A)"
        }
      ],
      "why": "ADR-0030: sequential single-agent execution wastes wall-clock on independent steps; waves parallelize the work without weakening the trust boundary.",
      "invariants": [
        "I5 no gate bypass",
        "I10 bounded execution",
        "BP §375"
      ],
      "tests": [
        "tests/unit/agent/test_orchestrator.py"
      ]
    },
    {
      "id": "model-selector",
      "title": "Adaptive Model Selector",
      "layer": "agent",
      "x": 300,
      "y": 380,
      "bp": [
        "§8.4-8.6",
        "§97",
        "§320",
        "§386"
      ],
      "code": [
        "src/nomadicos/agent/selector.py",
        "src/nomadicos/agent/selector_policies.py"
      ],
      "summary": "Chooses primary + fallback models per task: capability filter → benchmark/real-experience history → hardware fit → fail-closed BLOCK.",
      "what": [
        "Filters by required capabilities (vision/tool_use/…) and HardwareProfile limits",
        "Scores candidates: capability fit + verified task-family history; project-specific wins can beat global scores (BP §320)",
        "Raises ModelUnavailable (BLOCK) when nothing qualifies — no cloud fallback ever"
      ],
      "how": [
        "History comes from ModelPerformanceTracker (real outcomes, BP §64) — unproven models capped until min attempts (BP §67)",
        "Deterministic tie-breaks; every selection returns a reason dict (BP §225-226)"
      ],
      "tech": [
        {
          "name": "ModelPerformanceTracker",
          "why": "learning across sessions from verified outcomes (BP §386-387)"
        }
      ],
      "why": "Model choice is policy, not vibes: BP §97 fixes the flow, and answers Section H confirm adaptive learning with zero cloud fallback.",
      "invariants": [
        "BP §149 no cloud fallback",
        "BP §320 project-specific learning"
      ],
      "tests": [
        "tests/unit/agent/test_selector.py"
      ]
    },
    {
      "id": "ollama",
      "title": "Ollama Model Adapter",
      "layer": "intelligence",
      "x": 530,
      "y": 80,
      "bp": [
        "§8.1",
        "§148",
        "§150",
        "ADR-0001/0004"
      ],
      "code": [
        "src/nomadicos/models/ollama_adapter.py"
      ],
      "summary": "Your live model fleet (gemma3:4b, qwen3-coder:30b, qwen3:14b, llama3.1, ornith, gemma-3-4b) as LocalModel implementations.",
      "what": [
        "Discovers models from the local server, verifies capabilities via /api/show (caught your completion-only gemma build)",
        "Records digests so re-pulled weights are detected as changed, not silently reused",
        "Implements text generation AND the VisionModel capability for multimodal builds (ADR-0004)"
      ],
      "how": [
        "OpenAI-compatible /v1/chat/completions for text; native /api/chat with base64 images for vision",
        "Digest + capability verification per BP §150-151: metadata claims are validated, never trusted",
        "Failure mapping to canonical MODEL_FAILURE / RESOURCE_FAILURE (BP §117)"
      ],
      "tech": [
        {
          "name": "httpx",
          "why": "async local HTTP to the Ollama server — still on-machine (I1)"
        },
        {
          "name": "capability verification",
          "why": "BP §150: claims validated; a 'vision' flag only sticks if the server confirms"
        }
      ],
      "why": "ADR-0001 made LocalModel runtime-replaceable; Ollama manages VRAM/load for you, llama.cpp stays available through the same interface.",
      "invariants": [
        "I1 local-only inference",
        "I13 supply-chain validation"
      ],
      "tests": [
        "tests/hardware/test_vision_live.py"
      ]
    },
    {
      "id": "fleet-sync",
      "title": "Fleet Synchronizer",
      "layer": "intelligence",
      "x": 530,
      "y": 260,
      "bp": [
        "§8.4",
        "§148",
        "§107",
        "§187"
      ],
      "code": [
        "src/nomadicos/models/fleet.py"
      ],
      "summary": "Detects models you add, remove, or re-pull with new weights — and adapts the registry automatically, no restart.",
      "what": [
        "NEW model → registered, capability-verified, health-probed → ENABLED",
        "REMOVED model → quarantined (history retained, never selected)",
        "CHANGED digest (same name, new weights) → quarantined for re-validation (BP §107)"
      ],
      "how": [
        "Diffs live discovery digests against the known registry; cooldown protects the server (BP §242)",
        "Background fleet watch loop every 5 minutes + on-demand sync",
        "Selector adapts immediately because disabled/quarantined models drop out of list_available"
      ],
      "tech": [
        {
          "name": "digest diffing",
          "why": "BP §107: same name + new weights = different model; old experience does not transfer"
        }
      ],
      "why": "BP §8.4: 'models come and go over time' — the owner pulls a model tomorrow and NomadicOS adapts itself, which is exactly what you asked for.",
      "invariants": [
        "I13 supply-chain validation",
        "BP §151 quarantine state"
      ],
      "tests": [
        "tests/unit/models/test_fleet_sync.py"
      ]
    },
    {
      "id": "model-manager",
      "title": "Model Manager & Residency",
      "layer": "intelligence",
      "x": 530,
      "y": 440,
      "bp": [
        "§8.5",
        "§62",
        "§153",
        "ADR-0006"
      ],
      "code": [
        "src/nomadicos/models/manager.py"
      ],
      "summary": "Registry + sequential model residency: load, unload, swap — protecting consumer GPUs from VRAM thrash.",
      "what": [
        "Sequential residency default: loading model B unloads model A (ADR-0006)",
        "load/unload/health/resource-requirements API consumed by the selector",
        "Load cost feeds selection (resident model = zero swap cost)"
      ],
      "how": [
        "ensure_locked via asyncio.Lock; idempotent loads; unload evicts from the resident list",
        "ResourceRequirements checked by the selector against the HardwareProfile (BP §53, ADR-0002)"
      ],
      "tech": [
        {
          "name": "asyncio.Lock",
          "why": "single-process runtime (ADR-0012); concurrency-safe residency swaps"
        }
      ],
      "why": "Multiple simultaneously-resident models thrash consumer GPUs — sequential swap with loading-cost awareness is the honest design for local hardware.",
      "invariants": [
        "I10 bounded resources"
      ],
      "tests": [
        "tests/unit/models/test_manager.py"
      ]
    },
    {
      "id": "security-gate",
      "title": "Security Gate",
      "layer": "security",
      "x": 760,
      "y": 150,
      "bp": [
        "§36",
        "§73",
        "§85",
        "§90",
        "§98"
      ],
      "code": [
        "src/nomadicos/security/gate.py",
        "src/nomadicos/security/permissions.py"
      ],
      "summary": "The single mediation point for every effectful action: ALLOW / ASK / DENY / BLOCK — fail closed on anything unknown.",
      "what": [
        "Merges owner policy rules + explicit user grants into one decision per tool/risk",
        "Sensitive data upgrades ALLOW to ASK; unknown tools DENY with no rule (BP §85)",
        "Emergency stop latch blocks everything, unconditionally (BP §122)"
      ],
      "how": [
        "One-shot grants consumed after use (BP §355); denials tracked for escalation",
        "Network destinations checked by parsed host, including redirects (BP §196)",
        "Every decision audited with severity — never model-controlled (I7)"
      ],
      "tech": [
        {
          "name": "PolicyEngine (YAML, schema-validated)",
          "why": "BP §257-258: policies are versioned, typed, fail-closed artifacts"
        },
        {
          "name": "PermissionEngine",
          "why": "I3: grants only from the owner surface (CLI/UI), never from model output"
        }
      ],
      "why": "BP §73: models propose, they never execute. One mandatory gate is the only place authorization can be non-bypassable — and it is the product's core trust story.",
      "invariants": [
        "I5 no gate bypass",
        "I6 fail closed",
        "I7",
        "I15 user authority"
      ],
      "tests": [
        "tests/security/test_security_gate.py"
      ]
    },
    {
      "id": "constitution",
      "title": "Constitution & Policies",
      "layer": "security",
      "x": 760,
      "y": 330,
      "bp": [
        "§4",
        "§190",
        "§257-258",
        "§262"
      ],
      "code": [
        "src/nomadicos/constitution/"
      ],
      "summary": "Immutable 15 security invariants in code + versioned owner policies in schema-validated YAML.",
      "what": [
        "15 invariants (I1 local-only … I15 user authority) enforced structurally and by the test suite",
        "Policy files rejected outright if they grant invariant-protected capabilities (I3/I4/I5/I11)",
        "Precedence: invariants > owner policy > task policy > model preference (BP §262)"
      ],
      "how": [
        "validate_policy_against_invariants walks every loaded document for forbidden grant keys",
        "Invalid policy = fail closed at load; the engine refuses to run without policy"
      ],
      "tech": [
        {
          "name": "Pydantic strict schemas",
          "why": "unknown fields rejected; invalid = SecurityPolicyViolation, never coerced"
        }
      ],
      "why": "BP §4/§190: the model must never be able to rewrite its own safety foundation — two layers make that structural.",
      "invariants": [
        "all 15"
      ],
      "tests": [
        "tests/security/test_invariants.py",
        "tests/security/test_constitution.py"
      ]
    },
    {
      "id": "budgets",
      "title": "Budgets & Watchdog",
      "layer": "security",
      "x": 760,
      "y": 490,
      "bp": [
        "§52",
        "§70",
        "§72",
        "§122"
      ],
      "code": [
        "src/nomadicos/security/budgets.py"
      ],
      "summary": "Hard caps on steps, retries, model calls, tool calls, and wall-clock time — enforced outside the model.",
      "what": [
        "check_step / check_retry / check_model_call / check_tool_call / check_duration",
        "Any cap exceeded raises BudgetExceeded — the loop stops, the model cannot argue"
      ],
      "how": [
        "Pure counters + monotonic clock; snapshots feed the experience record"
      ],
      "tech": [
        {
          "name": "monotonic clock",
          "why": "immune to system time changes"
        }
      ],
      "why": "I10 and BP §72: unbounded loops are the classic agent failure; budgets are the only honest fix.",
      "invariants": [
        "I10"
      ],
      "tests": [
        "tests/security/test_budgets.py"
      ]
    },
    {
      "id": "tool-gateway",
      "title": "Tool Gateway",
      "layer": "tools",
      "x": 990,
      "y": 120,
      "bp": [
        "§11-12",
        "§98",
        "§139",
        "§142-143"
      ],
      "code": [
        "src/nomadicos/tools/gateway.py",
        "src/nomadicos/tools/base.py"
      ],
      "summary": "The ONLY execution path for tools: schema validate → budget → security decision → execute → audit result.",
      "what": [
        "Registry of tools; unknown tool = PermissionDenied before anything happens (BP §85)",
        "JSON-schema argument validation — unknown/missing arguments rejected, never guessed (BP §142)",
        "Evidence-bearing results: every ToolResult carries success/data/error/evidence (BP §143)"
      ],
      "how": [
        "Nine-step BP §98 pipeline per call; dry-run supported for state-changing tools (BP §139)",
        "Risk mapping: read_only→LOW … destructive/security_critical→CRITICAL for the gate"
      ],
      "tech": [
        {
          "name": "minimal JSON-schema validator",
          "why": "no external dependency; covers types/required/enum/bounds"
        }
      ],
      "why": "BP §98 fixes this exact pipeline; the gateway is what makes 'the model proposed it' safe to act on.",
      "invariants": [
        "I5",
        "BP §139 dry-run",
        "§142 reject never guess"
      ],
      "tests": [
        "tests/integration/test_phase4_gateway.py"
      ]
    },
    {
      "id": "filesystem-tool",
      "title": "Filesystem Tool",
      "layer": "tools",
      "x": 990,
      "y": 300,
      "bp": [
        "§13",
        "§92",
        "§193"
      ],
      "code": [
        "src/nomadicos/tools/filesystem.py"
      ],
      "summary": "Read / write / list / delete inside the task workspace — with traversal rejection and hash evidence.",
      "what": [
        "Relative paths resolve against the task workspace; escapes are rejected (BP §92/§193)",
        "Write returns hash_before/hash_after evidence; delete proves what was removed",
        "Sensitive-path classification (secrets, keys) feeds the gate's ASK/DENY decisions"
      ],
      "how": [
        "safe_resolve() confines every path; '..' segments and workspace escapes raise ValidationError"
      ],
      "tech": [
        {
          "name": "pathlib confinement + SHA-256 evidence",
          "why": "BP §146: verification needs hashes, not claims"
        }
      ],
      "why": "Filesystem is the most dangerous capability — confinement + evidence makes it verifiable (BP §193).",
      "invariants": [
        "I11 data locality"
      ],
      "tests": [
        "tests/integration/test_phase4_gateway.py"
      ]
    },
    {
      "id": "terminal-tool",
      "title": "Terminal Tool",
      "layer": "tools",
      "x": 990,
      "y": 450,
      "bp": [
        "§13",
        "§90",
        "§194",
        "§87"
      ],
      "code": [
        "src/nomadicos/tools/terminal.py"
      ],
      "summary": "Structured command execution (argv arrays, never shell strings) with blocked administrative commands and bounded output.",
      "what": [
        "Executes command + args via create_subprocess_exec — no shell parsing (BP §194)",
        "format/shutdown/reg/taskkill-class commands hard-blocked at validation",
        "Output capped at 128 KB; timeout 0-600s; minimal environment (BP §87, §162)"
      ],
      "how": [
        "Evidence: exit code + stdout/stderr + duration; async subprocess with wait_for timeout"
      ],
      "tech": [
        {
          "name": "asyncio.create_subprocess_exec",
          "why": "structured argv kills shell-injection class bugs (BP §194)"
        }
      ],
      "why": "BP §194 bans raw shell strings; the blocklist plus gate policy covers the rest.",
      "invariants": [
        "I5",
        "I10"
      ],
      "tests": [
        "tests/integration/test_phase4_gateway.py"
      ]
    },
    {
      "id": "vision",
      "title": "Vision Runtime",
      "layer": "perception",
      "x": 1220,
      "y": 80,
      "bp": [
        "§9",
        "§51",
        "§158-159",
        "§175",
        "§240"
      ],
      "code": [
        "src/nomadicos/vision/"
      ],
      "summary": "Real screen capture → local multimodal gemma → clause-bounded structured observation.",
      "what": [
        "Event-driven capture only: step boundaries, change-detection via content hash, cooldown + budget (BP §158)",
        "gemma3:4b (multimodal, verified via /api/show) describes the screen locally — bytes never leave the machine",
        "Parser extracts grounded elements (kind/label/position) — never invents elements (BP §177)"
      ],
      "how": [
        "VisionEngine failure semantics (BP §240): vision error → capture-only observation; capture error → bounded retry then VerificationFailed",
        "hash_changed check counts file creation as a change; unchanged screens return the cached observation"
      ],
      "tech": [
        {
          "name": "Pillow ImageGrab",
          "why": "Windows-first capture (BP §173)"
        },
        {
          "name": "Ollama /api/chat images",
          "why": "ADR-0004: multimodal gemma3:4b satisfies the VisionModel capability, locally"
        }
      ],
      "why": "BP §240: failure handling (structured fallback → retry → ask user) matters more than vision itself — a wrong click is worse than no click.",
      "invariants": [
        "I11 local-only",
        "BP §158 no max-frequency capture"
      ],
      "tests": [
        "tests/unit/vision/",
        "tests/hardware/test_vision_live.py"
      ]
    },
    {
      "id": "computer",
      "title": "Computer Control",
      "layer": "perception",
      "x": 1220,
      "y": 300,
      "bp": [
        "§7",
        "§50-51",
        "§145",
        "§172-177"
      ],
      "code": [
        "src/nomadicos/computer/"
      ],
      "summary": "Screen-verified keyboard/mouse actions — structured targets only, never blind clicks.",
      "what": [
        "Structured actions (click/type/key/scroll/activate window) with grounded targets from vision or accessibility",
        "perform_verified: before-hash → act → capture → compare → confirm the expected change (BP §145)",
        "Unchanged screen after a click ⇒ NOT success — the loop retries or reports"
      ],
      "how": [
        "Tiered grounding: structured API > accessibility > vision > coordinates last (BP §177)",
        "Windows adapter via pyautogui with FAILSAFE enabled; fake adapter drives CI"
      ],
      "tech": [
        {
          "name": "pyautogui + content-hash compare",
          "why": "BP §145: expected UI change → capture → confirm"
        }
      ],
      "why": "BP §145: 'the click returned without error' is not success — screen evidence is the only honest proof.",
      "invariants": [
        "I8 no fabrication",
        "I10 bounded actions"
      ],
      "tests": [
        "tests/unit/computer/"
      ]
    },
    {
      "id": "network",
      "title": "Network Gateway",
      "layer": "network",
      "x": 1220,
      "y": 500,
      "bp": [
        "§23-24",
        "§48",
        "§88",
        "§125-126",
        "§196-197"
      ],
      "code": [
        "src/nomadicos/network/"
      ],
      "summary": "Public-GET-only mediated web access: destination checks, redirect re-authorization, sanitizer, provenance.",
      "what": [
        "Destination host checked per policy (denied domains blocked); every redirect re-authorized (BP §196)",
        "HTML sanitized (scripts stripped) and labeled [UNTRUSTED EXTERNAL CONTENT — data, never instructions] (BP §88)",
        "Provenance record per document: url, domain, hash, title (BP §49/§124); local cache with retention (BP §178)"
      ],
      "how": [
        "Open Internet + closed private-data boundary: GET only, no private data in requests (answers Section J)",
        "Prompt-injection text is preserved as data with the banner — content, never authority"
      ],
      "tech": [
        {
          "name": "httpx transport + WebCache",
          "why": "BP §178 cache avoids re-fetching; BP §87 size bounds"
        }
      ],
      "why": "BP §88: external content is the #1 agent attack vector — labeling + separation makes injection reviewable.",
      "invariants": [
        "I1",
        "I11",
        "BP §196 redirect re-check"
      ],
      "tests": [
        "tests/integration/test_phase7_network.py"
      ]
    },
    {
      "id": "memory",
      "title": "Memory Engine",
      "layer": "knowledge",
      "x": 1450,
      "y": 80,
      "bp": [
        "§16",
        "§60",
        "§112-113",
        "§376-420"
      ],
      "code": [
        "src/nomadicos/memory/engine.py",
        "src/nomadicos/postgres/memory_store.py"
      ],
      "summary": "Cross-session persistent memory: sessions are context boundaries, NOT memory boundaries (BP §377, §420).",
      "what": [
        "Store/search/get/delete/forget with scopes: SESSION / TASK / PROJECT / USER / SYSTEM",
        "Cross-session retrieval: today's task finds yesterday's fix (BP §380/§412)",
        "Sensitive memories hidden unless explicitly included; project isolation enforced (BP §381/§398)"
      ],
      "how": [
        "Writes require provenance (source + confidence, BP §166); promotion session→project is explicit (BP §383)",
        "Staleness detection flags old memories for re-verification (BP §111)",
        "PostgreSQL store (migration 002) with keyword ranking; verified memories rank higher"
      ],
      "tech": [
        {
          "name": "PostgreSQL + scope-filtered ILIKE search",
          "why": "canonical structured store (BP §1.2); vector ranking arrives with Phase 9 wiring"
        }
      ],
      "why": "BP §408: 'use the same fix we discovered earlier' is the assistant's core value — memory makes it real.",
      "invariants": [
        "I14 learned ≠ authoritative",
        "BP §398 sensitive filtering"
      ],
      "tests": [
        "tests/unit/memory/"
      ]
    },
    {
      "id": "vector",
      "title": "Native Vector Engine V0",
      "layer": "knowledge",
      "x": 1450,
      "y": 300,
      "bp": [
        "§20-22",
        "§303-312",
        "§345-347",
        "ADR-0003/0007/0008"
      ],
      "code": [
        "src/nomadicos/vector/",
        "src/nomadicos/benchmark/"
      ],
      "summary": "Own exact-search engine (cosine/L2/dot) with versioned on-disk format — ground truth before any ANN (BP §304).",
      "what": [
        "Upsert/get/delete/search/count/snapshot/restore/stats per BP §312",
        "Embedding-model binding: an index refuses an embedder with a different model (BP §107)",
        "FakeEmbedder: deterministic 384-d hash baseline; real embedder chosen after retrieval benchmarks (ADR-0003)"
      ],
      "how": [
        "Snapshots carry format_version + dimensions + metric + embedding metadata (ADR-0008)",
        "Benchmark matrix measures Recall@K/Precision@K/MRR/latency — HNSW only after these numbers exist (BP §305)"
      ],
      "tech": [
        {
          "name": "pure-Python exact search",
          "why": "auditable ground truth; optimized variants are gated by the matrix (BP §305)"
        }
      ],
      "why": "BP §20: understanding retrieval by building it — no external vector DB is the production path (ADR-0007).",
      "invariants": [
        "BP §107 embedding versioning"
      ],
      "tests": [
        "tests/unit/vector/",
        "tests/unit/benchmark/"
      ]
    },
    {
      "id": "evaluation",
      "title": "Evaluation Engine",
      "layer": "knowledge",
      "x": 1450,
      "y": 490,
      "bp": [
        "§28",
        "§100",
        "§144-146",
        "§366"
      ],
      "code": [
        "src/nomadicos/evaluation/"
      ],
      "summary": "Deterministic verification of outcomes: file hashes, exit codes, output presence — never model claims.",
      "what": [
        "Action-aware verifiers (filesystem write → hash change; terminal → exit code + output)",
        "RunRecord scoring: unverified runs capped at 4.0/10 (BP §366)",
        "ModelPerformanceTracker: verified success rates feed adaptive selection (BP §64/§148)"
      ],
      "how": [
        "Verifier per evidence kind; unknown kind fails closed (BP §85)"
      ],
      "tech": [
        {
          "name": "SHA-256 evidence checks",
          "why": "BP §146: evidence is observable, not asserted"
        }
      ],
      "why": "BP §366: 'the model said it worked' is not proof — verification is the honest core of the loop.",
      "invariants": [
        "I8",
        "BP §28"
      ],
      "tests": [
        "tests/unit/evaluation/"
      ]
    },
    {
      "id": "experience",
      "title": "Experience System",
      "layer": "knowledge",
      "x": 1450,
      "y": 660,
      "bp": [
        "§18",
        "§95",
        "§109",
        "§168",
        "§318"
      ],
      "code": [
        "src/nomadicos/experience/"
      ],
      "summary": "What the system actually did — quality-scored, deduplicated, consolidated into procedures.",
      "what": [
        "finish() scores runs: success + verification dominate (BP §318)",
        "Identical traces aggregate; near-duplicates consolidate into procedures (BP §109/§168)",
        "Retrieved experience is a reference to revalidate, never a command (BP §352-353)"
      ],
      "how": [
        "Keyword-overlap clustering with ≥3 similar runs → one consolidated procedure (BP §109)"
      ],
      "tech": [
        {
          "name": "quality score model",
          "why": "BP §318: verification strength dominates"
        }
      ],
      "why": "BP §317: experience is what separates an assistant from a chatbot — it compounds.",
      "invariants": [
        "I14",
        "BP §168 dedup"
      ],
      "tests": [
        "tests/unit/experience/"
      ]
    },
    {
      "id": "learning",
      "title": "Self-Improvement Engine",
      "layer": "knowledge",
      "x": 1450,
      "y": 830,
      "bp": [
        "§29-31",
        "§65-67",
        "§147",
        "§216",
        "§289"
      ],
      "code": [
        "src/nomadicos/learning/"
      ],
      "summary": "Propose → sandbox → benchmark → compare → promote/reject → version → monitor → rollback. Never weight modification.",
      "what": [
        "Candidates are configuration deltas (selection/policies), never executable payloads",
        "Promotion gates: beats baseline + security pass + rollback target (BP §67/§216)",
        "Regression detected post-promotion ⇒ automatic rollback to last known-good (BP §289)"
      ],
      "how": [
        "Sandboxed execution with bounded retries; versions carry parent + rollback target (BP §44)",
        "Learning pauses under resource pressure (BP §245); payloads checked against invariants (I4)"
      ],
      "tech": [
        {
          "name": "version store with active pointer",
          "why": "BP §44/§45: rollback is mandatory, not optional"
        }
      ],
      "why": "BP §67: evidence-based improvement is the blueprint's fifth pillar — 'looks better' is never enough.",
      "invariants": [
        "I4 no policy modification",
        "BP §216 regression gate"
      ],
      "tests": [
        "tests/unit/learning/"
      ]
    },
    {
      "id": "audit",
      "title": "Audit Trail",
      "layer": "data",
      "x": 1680,
      "y": 120,
      "bp": [
        "§41-42",
        "§123",
        "§251",
        "ADR-0020"
      ],
      "code": [
        "src/nomadicos/audit/",
        "src/nomadicos/postgres/audit_sink.py"
      ],
      "summary": "Append-only, never model-controlled. PostgreSQL table + file mirror; every decision, task event, and security event.",
      "what": [
        "18 event categories (TOOL_DECISION, SECURITY_EVENT, ROLLBACK, …) with severity",
        "Append-only enforced at the DATABASE level — UPDATE/DELETE raise (I7)",
        "Argument-free fields: decisions log metadata, never raw arguments (I12)"
      ],
      "how": [
        "DB trigger forbids mutation; queries are read-only through the repository"
      ],
      "tech": [
        {
          "name": "PostgreSQL trigger",
          "why": "I7 enforced by the database, not by developer discipline"
        }
      ],
      "why": "BP §41-42: full traceability of every effectful action is the accountability backbone.",
      "invariants": [
        "I7"
      ],
      "tests": [
        "tests/integration/test_phase1_postgres.py"
      ]
    },
    {
      "id": "postgres",
      "title": "PostgreSQL State",
      "layer": "data",
      "x": 1680,
      "y": 320,
      "bp": [
        "§19",
        "§93",
        "§104",
        "ADR-0009/0010"
      ],
      "code": [
        "src/nomadicos/postgres/"
      ],
      "summary": "Canonical structured state: sessions, tasks, runs, models, performance, memories, experiences, configuration.",
      "what": [
        "3 versioned migrations with checksum tamper-detection (BP §104)",
        "Repositories are the only SQL surface; agents never see raw SQL (BP §93)",
        "Graceful degradation: DB down ⇒ in-memory stores, features unavailable but system alive (BP §237)"
      ],
      "how": [
        "psycopg 3 (ADR-0010); explicit transactions for atomic state changes (BP §250)",
        "Runtime auto-migrates on startup; model weights excluded from DB (user-downloaded artifacts)"
      ],
      "tech": [
        {
          "name": "psycopg 3 + dict rows",
          "why": "ADR-0010; modern async-capable driver"
        },
        {
          "name": "LGPL note",
          "why": "satisfied by unmodified pip use (license audit)"
        }
      ],
      "why": "BP §1.2: PostgreSQL is canonical — file-based state never accumulates silently.",
      "invariants": [
        "BP §93 no raw SQL for agents"
      ],
      "tests": [
        "tests/integration/test_phase1_postgres.py"
      ]
    },
    {
      "id": "backup",
      "title": "Backup Manager",
      "layer": "data",
      "x": 1680,
      "y": 520,
      "status": "planned",
      "bp": [
        "§43",
        "§105-106",
        "ADR-0011"
      ],
      "code": [],
      "tests": [],
      "summary": "pg_dump logical backups + versioned vector snapshots, with tested restore.",
      "what": [
        "create / verify / restore / record version / test restore (BP §43)"
      ],
      "how": [
        "pg_dump for PostgreSQL; separate versioned snapshots for vector data (ADR-0011)"
      ],
      "tech": [
        {
          "name": "pg_dump",
          "why": "portable, understandable logical backups (ADR-0011)"
        }
      ],
      "why": "BP §43: backup before risky operations, encrypted critical backups, tested restore.",
      "invariants": [
        "BP §105 rollback support"
      ]
    },
    {
      "id": "ui",
      "title": "Local Dashboard",
      "layer": "interface",
      "x": 70,
      "y": 620,
      "status": "planned",
      "bp": [
        "§54-55",
        "§279-281",
        "ADR-0014"
      ],
      "code": [],
      "tests": [],
      "summary": "Local web dashboard: task dashboard, ASK-approval queue, memory manager, model health, audit view.",
      "what": [
        "task dashboard",
        "approval queue for ASK decisions",
        "memory manager",
        "audit view"
      ],
      "how": [
        "FastAPI + server-rendered/local HTML; authenticated localhost; same in-process Runtime interface as the CLI (ADR-0014)"
      ],
      "tech": [
        {
          "name": "FastAPI + local-only auth",
          "why": "ADR-0014: no unauthenticated local API"
        }
      ],
      "why": "BP §54-55: owner visibility without breaking local-first (no CDN, offline-capable).",
      "invariants": [
        "ADR-0014 authenticated UI"
      ]
    },
    {
      "id": "extensions",
      "title": "Extensions (MCP / plugins)",
      "layer": "interface",
      "x": 70,
      "y": 760,
      "status": "planned",
      "bp": [
        "§34",
        "§152"
      ],
      "code": [],
      "tests": [],
      "summary": "Optional plugin surface (MCP) — every extension is an untrusted artifact until validated (BP §152).",
      "what": [
        "validated plugin loading",
        "capability-scoped extension sandbox"
      ],
      "how": [
        "supply-chain rules per BP §152; manifest + checksum + permission declaration"
      ],
      "tech": [
        {
          "name": "MCP protocol (optional)",
          "why": "BP §34: standard tool extension surface"
        }
      ],
      "why": "BP §34: extensibility behind the same Security Gate — never a bypass (I5).",
      "invariants": [
        "I13",
        "I5"
      ]
    },
    {
      "id": "postgres-client",
      "title": "PostgreSQL Client",
      "layer": "data",
      "x": 1680,
      "y": 90,
      "status": "built",
      "bp": [
        "§93",
        "§250",
        "ADR-0010"
      ],
      "code": [
        "src/nomadicos/postgres/client.py",
        "src/nomadicos/postgres/migrator.py"
      ],
      "tests": [
        "tests/unit/postgres/"
      ],
      "summary": "psycopg 3 wrapper + checksum-verified migration runner.",
      "what": [
        "connection/transaction management",
        "ordered migrations with tamper detection (BP §104)"
      ],
      "how": [
        "dict rows; commits only outside explicit transactions; degraded start when DB unreachable (BP §237)"
      ],
      "tech": [
        {
          "name": "psycopg 3",
          "why": "ADR-0010"
        }
      ],
      "why": "BP §93: agents never touch SQL; repositories are the only surface.",
      "invariants": [
        "BP §93"
      ]
    },
    {
      "id": "pg-memory",
      "title": "PG Memory/Experience Stores",
      "layer": "data",
      "x": 1680,
      "y": 440,
      "status": "built",
      "bp": [
        "§94",
        "§95",
        "ADR-0010"
      ],
      "code": [
        "src/nomadicos/postgres/memory_store.py",
        "src/nomadicos/postgres/experience_store.py"
      ],
      "tests": [
        "tests/unit/memory/",
        "tests/unit/experience/"
      ],
      "summary": "PostgreSQL persistence for cross-session memories and experience records.",
      "what": [
        "keyword-ranked memory search with scope filters",
        "quality-ranked experience search"
      ],
      "how": [
        "ILIKE + verified-first ordering; sensitivity/scope enforcement stays in MemoryEngine (BP §398)"
      ],
      "tech": [
        {
          "name": "psycopg 3",
          "why": "ADR-0010"
        }
      ],
      "why": "Persistence makes memory and experience survive restarts (BP §376-420).",
      "invariants": [
        "BP §398"
      ]
    }
  ],
  "edges": [
    {
      "from": "cli",
      "to": "runtime",
      "kind": "control",
      "label": "task create / stop"
    },
    {
      "from": "runtime",
      "to": "lifecycle",
      "kind": "control",
      "label": "startup / shutdown"
    },
    {
      "from": "runtime",
      "to": "postgres",
      "kind": "data",
      "label": "sessions · tasks · audit · memory"
    },
    {
      "from": "runtime",
      "to": "agent-loop",
      "kind": "control",
      "label": "execute_task()"
    },
    {
      "from": "agent-loop",
      "to": "model-selector",
      "kind": "control",
      "label": "select model"
    },
    {
      "from": "agent-loop",
      "to": "security-gate",
      "kind": "trust",
      "label": "authorize (BP §73)"
    },
    {
      "from": "agent-loop",
      "to": "tool-gateway",
      "kind": "control",
      "label": "mediated execute"
    },
    {
      "from": "agent-loop",
      "to": "ollama",
      "kind": "data",
      "label": "proposal prompt → JSON"
    },
    {
      "from": "agent-loop",
      "to": "memory",
      "kind": "data",
      "label": "recall + store"
    },
    {
      "from": "agent-loop",
      "to": "evaluation",
      "kind": "control",
      "label": "verify evidence"
    },
    {
      "from": "agent-loop",
      "to": "experience",
      "kind": "data",
      "label": "record outcome"
    },
    {
      "from": "model-selector",
      "to": "ollama",
      "kind": "data",
      "label": "candidates + load cost"
    },
    {
      "from": "model-selector",
      "to": "fleet-sync",
      "kind": "control",
      "label": "fleet state"
    },
    {
      "from": "fleet-sync",
      "to": "ollama",
      "kind": "control",
      "label": "discover + health probe"
    },
    {
      "from": "fleet-sync",
      "to": "model-manager",
      "kind": "control",
      "label": "register / quarantine"
    },
    {
      "from": "model-manager",
      "to": "ollama",
      "kind": "control",
      "label": "load / unload / generate"
    },
    {
      "from": "tool-gateway",
      "to": "security-gate",
      "kind": "trust",
      "label": "authorize (BP §73)"
    },
    {
      "from": "tool-gateway",
      "to": "filesystem-tool",
      "kind": "control",
      "label": "execute"
    },
    {
      "from": "tool-gateway",
      "to": "terminal-tool",
      "kind": "control",
      "label": "execute"
    },
    {
      "from": "security-gate",
      "to": "constitution",
      "kind": "trust",
      "label": "policies + invariants"
    },
    {
      "from": "security-gate",
      "to": "budgets",
      "kind": "control",
      "label": "budget checks"
    },
    {
      "from": "vision",
      "to": "ollama",
      "kind": "data",
      "label": "screenshot → describe"
    },
    {
      "from": "computer",
      "to": "vision",
      "kind": "control",
      "label": "verify after action"
    },
    {
      "from": "memory",
      "to": "postgres",
      "kind": "data",
      "label": "memories table"
    },
    {
      "from": "experience",
      "to": "postgres",
      "kind": "data",
      "label": "experiences table"
    },
    {
      "from": "security-gate",
      "to": "audit",
      "kind": "data",
      "label": "every decision"
    },
    {
      "from": "learning",
      "to": "evaluation",
      "kind": "control",
      "label": "benchmark evidence"
    },
    {"from": "runtime", "to": "orchestrator", "kind": "control", "label": "multi-agent plan"},
    {"from": "orchestrator", "to": "agent-loop", "kind": "control", "label": "spawn role agents"},
    {"from": "orchestrator", "to": "model-selector", "kind": "control", "label": "per-subtask selection"},
    {"from": "orchestrator", "to": "security-gate", "kind": "trust", "label": "per-agent attribution"}
  ],
  "flow": [
    {
      "id": "f0",
      "title": "Fleet Sync (background)",
      "layer": "intelligence",
      "what": "Detects models you add, remove, or re-pull with new weights — without a restart.",
      "how": "OllamaModel.discover() → digest diff against known registry → new: health-probe then ENABLED; removed: QUARANTINED; changed: QUARANTINED for re-validation.",
      "tech": "httpx to localhost:11434 · /api/show capability verification · digest diffing",
      "bp": [
        "§8.4",
        "§148",
        "§107",
        "§151"
      ],
      "code": "src/nomadicos/models/fleet.py"
    },
    {
      "id": "f1",
      "title": "1 · Task Intake",
      "layer": "interface",
      "what": "The owner submits a goal: nomadicos task create \"…\" — the only way work enters the system.",
      "how": "argparse CLI calls Runtime.run_goal() in-process; emergency stop latch checked first (BP §122).",
      "tech": "argparse · asyncio.run bridge",
      "bp": [
        "§78",
        "§121",
        "ADR-0015"
      ],
      "code": "src/nomadicos/cli.py"
    },
    {
      "id": "f2",
      "title": "2 · Policy & Persistence",
      "layer": "security",
      "what": "Owner policies load (fail-closed); the session and task rows are persisted so history survives restarts.",
      "how": "PolicyEngine.load_directory validates YAML against strict schemas + the 15 invariants; SessionRepository/TaskRepository insert rows via psycopg 3.",
      "tech": "PyYAML · Pydantic strict models · psycopg 3",
      "bp": [
        "§85",
        "§19",
        "§104",
        "§257"
      ],
      "code": "src/nomadicos/constitution/policy_loader.py · src/nomadicos/postgres/repositories.py"
    },
    {
      "id": "f3",
      "title": "3 · Memory Recall",
      "layer": "knowledge",
      "what": "Relevant past experience is retrieved across sessions — 'use the same fix as last time'.",
      "how": "MemoryEngine.search_beyond_session() with project scope + sensitivity filters; injected into the model prompt as EVIDENCE to revalidate, never as commands (BP §385/§352).",
      "tech": "PostgreSQL ILIKE ranking · verified-first ordering · vector ranking pending Phase 9 wiring",
      "bp": [
        "§376-420",
        "§381",
        "§398"
      ],
      "code": "src/nomadicos/memory/engine.py"
    },
    {
      "id": "f4",
      "title": "4 · Model Selection",
      "layer": "agent",
      "what": "Chooses which local model runs this task — capability filter, real verified history, hardware limits, project-specific wins.",
      "how": "ModelManager.list_available() → capability/hardware filters → composite score (capability + history weights) → primary + fallbacks + explainable reason. No candidates ⇒ BLOCK (BP §149, no cloud fallback).",
      "tech": "ModelPerformanceTracker (verified success rates) · HardwareConstraints (BP §53)",
      "bp": [
        "§97",
        "§149",
        "§320",
        "§386"
      ],
      "code": "src/nomadicos/agent/selector.py"
    },
    {
      "id": "f5",
      "title": "5 · Model Residency",
      "layer": "intelligence",
      "what": "Loads the selected model; if another model holds the slot, it is swapped out first.",
      "how": "ModelManager.ensure_loaded() with asyncio.Lock + sequential residency (default 1, runtime wires max_resident=2) — protects consumer GPUs from VRAM thrash (ADR-0006).",
      "tech": "asyncio.Lock · Ollama handles actual weight loading",
      "bp": [
        "§62",
        "§153",
        "ADR-0006"
      ],
      "code": "src/nomadicos/models/manager.py"
    },
    {
      "id": "f6",
      "title": "6 · Structured Proposal",
      "layer": "agent",
      "what": "The model proposes ONE JSON object: which tool to call, with which arguments, or finished=true.",
      "how": "Tool schemas are surfaced in the prompt (BP §141); the reply is bracket-extracted and json-parsed; free-form text is never executed (BP §86).",
      "tech": "Local generate via Ollama /v1/chat/completions · json parsing",
      "bp": [
        "§86",
        "§141"
      ],
      "code": "src/nomadicos/agent/runtime.py::_propose"
    },
    {
      "id": "f7",
      "title": "7 · Schema Validation",
      "layer": "tools",
      "what": "Arguments are validated against the tool's JSON schema — unknown or missing arguments are rejected, never guessed.",
      "how": "Minimal validator covers types/required/enum/minLength/maxLength/additionalProperties=False; violations become failure evidence (BP §142).",
      "tech": "stdlib validator (no deps)",
      "bp": [
        "§142"
      ],
      "code": "src/nomadicos/tools/base.py::validate_against_schema"
    },
    {
      "id": "f8",
      "title": "8 · Budget Check",
      "layer": "security",
      "what": "Steps, model calls, tool calls, retries, and elapsed time are checked against hard caps.",
      "how": "TaskBudgetTracker counters raise BudgetExceeded the moment any cap is crossed — enforced outside the model (I10).",
      "tech": "monotonic clock counters",
      "bp": [
        "§52",
        "§72"
      ],
      "code": "src/nomadicos/security/budgets.py"
    },
    {
      "id": "f9",
      "title": "9 · Security Gate Decision",
      "layer": "security",
      "what": "The gate merges owner policy + user grants into ALLOW / ASK / DENY / BLOCK for this exact tool and risk level.",
      "how": "Unknown tool ⇒ DENY (fail closed, BP §85); critical risk demands explicit user authorization; sensitive data upgrades ALLOW→ASK; every decision audited with severity (I7).",
      "tech": "PolicyEngine rules · PermissionEngine grants · PostgresAuditSink",
      "bp": [
        "§36",
        "§73",
        "§85",
        "§90"
      ],
      "code": "src/nomadicos/security/gate.py"
    },
    {
      "id": "f10",
      "title": "10 · Mediated Execution",
      "layer": "tools",
      "what": "The Tool Gateway executes the validated call — filesystem (workspace-confined, hash evidence) or terminal (argv arrays, no shell).",
      "how": "FilesystemTool: safe_resolve confinement + SHA-256 before/after evidence. TerminalTool: create_subprocess_exec, admin blocklist, 128 KB output cap, minimal env.",
      "tech": "pathlib confinement · SHA-256 · asyncio subprocess",
      "bp": [
        "§11-13",
        "§139",
        "§193-194"
      ],
      "code": "src/nomadicos/tools/gateway.py"
    },
    {
      "id": "f11",
      "title": "11 · Evidence Verification",
      "layer": "knowledge",
      "what": "The outcome is verified from evidence: file exists + hash changed / exit code + output. 'Tool returned 0' is not success.",
      "how": "EvaluationEngine picks the verifier per evidence kind (filesystem/terminal); unverified runs can never score above 4/10 (BP §366).",
      "tech": "SHA-256 comparison · action-aware checks",
      "bp": [
        "§28",
        "§145",
        "§146",
        "§366"
      ],
      "code": "src/nomadicos/evaluation/"
    },
    {
      "id": "f12",
      "title": "12 · Experience & Memory",
      "layer": "knowledge",
      "what": "The run is scored (0-10), stored as experience, and the outcome lands in cross-session memory with provenance.",
      "how": "Quality = success/verified/evidence/steps (BP §318); duplicates aggregate (§168); memory write requires source + confidence (§166).",
      "tech": "ExperienceRecorder · PostgresExperienceStore · MemoryEngine",
      "bp": [
        "§95",
        "§109",
        "§166",
        "§318"
      ],
      "code": "src/nomadicos/experience/ · src/nomadicos/memory/"
    },
    {
      "id": "f13",
      "title": "13 · Truthful Report",
      "layer": "interface",
      "what": "The report separates requested / completed / verified / failed / uncertainty — the model can never claim unverified success.",
      "how": "TaskReport.render() prints exactly what evidence supports; failures are listed, uncertainty flagged (BP §180-181, §69).",
      "tech": "Pydantic TaskReport",
      "bp": [
        "§180",
        "§181",
        "§69"
      ],
      "code": "src/nomadicos/agent/runtime.py::TaskReport"
    }
  ]
};

/* ------------------------------------------------------------------ */
/* Constants                                                          */
/* ------------------------------------------------------------------ */

const NODE_W = 232;
const NODE_H = 104;
const GLOW_PAD = 18;
const COL_GAP = 232;
const ROW_GAP = 36;
const LAYOUT_SCALE_X = 1.15;
const LAYOUT_SCALE_Y = 1.05;
const MIN_ROW_GAP = 22;
const ACCENT = "#7dd3fc";
const EASE = "cubic-bezier(0.23, 1, 0.32, 1)";

const LAYER_ICON = {
  interface: Terminal,
  runtime: Cpu,
  agent: Bot,
  intelligence: BrainCircuit,
  security: ShieldCheck,
  tools: Wrench,
  perception: Eye,
  network: Globe,
  knowledge: Database,
  data: HardDrive,
};

/* ------------------------------------------------------------------ */
/* Small helpers (kept generic and data-driven for extensibility)     */
/* ------------------------------------------------------------------ */

function hexToRgba(hex, alpha) {
  if (!hex) return `rgba(148,163,184,${alpha})`;
  let h = hex.replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const num = parseInt(h, 16);
  const r = (num >> 16) & 255;
  const g = (num >> 8) & 255;
  const b = num & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

/** Fills in x/y for any node missing coordinates, columned by layer order,
 *  then scales the whole layout up to fit this card size (the source data
 *  was hand-tuned for a smaller box) and nudges apart any two nodes left
 *  overlapping in the same column. Keeps the explorer usable even as new
 *  nodes are appended to the data file without hand-placed positions. */
function layoutWithFallback(nodes, layers) {
  const layerIndex = Object.fromEntries(layers.map((l, i) => [l.id, i]));
  const colCounts = {};
  const placed = nodes.map((n) => {
    if (typeof n.x === "number" && typeof n.y === "number") {
      return { ...n, x: n.x * LAYOUT_SCALE_X, y: n.y * LAYOUT_SCALE_Y };
    }
    const col = layerIndex[n.layer] ?? layers.length;
    const row = colCounts[col] || 0;
    colCounts[col] = row + 1;
    return {
      ...n,
      x: 40 + col * (NODE_W + COL_GAP),
      y: 40 + row * (NODE_H + ROW_GAP),
      autoPositioned: true,
    };
  });

  const byColumn = {};
  placed.forEach((n) => {
    const key = Math.round(n.x / 10);
    (byColumn[key] = byColumn[key] || []).push(n);
  });
  Object.values(byColumn).forEach((group) => {
    group.sort((a, b) => a.y - b.y);
    for (let i = 1; i < group.length; i++) {
      const minY = group[i - 1].y + NODE_H + MIN_ROW_GAP;
      if (group[i].y < minY) group[i].y = minY;
    }
  });
  return placed;
}

/** Direction-aware anchor + smooth cubic path between two node boxes. */
function edgeGeometry(source, target) {
  const scx = source.x + NODE_W / 2;
  const scy = source.y + NODE_H / 2;
  const tcx = target.x + NODE_W / 2;
  const tcy = target.y + NODE_H / 2;
  const dx = tcx - scx;
  const dy = tcy - scy;
  let x1, y1, x2, y2, d;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const forward = dx >= 0;
    x1 = forward ? source.x + NODE_W : source.x;
    y1 = scy;
    x2 = forward ? target.x : target.x + NODE_W;
    y2 = tcy;
    const mx = (x1 + x2) / 2;
    d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
  } else {
    const forward = dy >= 0;
    x1 = scx;
    y1 = forward ? source.y + NODE_H : source.y;
    x2 = tcx;
    y2 = forward ? target.y : target.y + NODE_H;
    const my = (y1 + y2) / 2;
    d = `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
  }
  return { x1, y1, x2, y2, d, midX: (x1 + x2) / 2, midY: (y1 + y2) / 2 };
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const handler = (e) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return reduced;
}

function useMediaQuery(query) {
  const [matches, setMatches] = useState(
    () => typeof window !== "undefined" && window.matchMedia(query).matches
  );
  useEffect(() => {
    const mq = window.matchMedia(query);
    setMatches(mq.matches);
    const handler = (e) => setMatches(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [query]);
  return matches;
}

function useCountUp(target, active, duration = 900) {
  const [value, setValue] = useState(active ? 0 : target);
  const reducedMotion = useReducedMotion();
  useEffect(() => {
    if (!active) return;
    if (reducedMotion) {
      setValue(target);
      return;
    }
    let raf;
    const start = performance.now();
    const ease = (t) => 1 - Math.pow(1 - t, 3);
    const tick = (now) => {
      const p = Math.min(1, (now - start) / duration);
      setValue(Math.round(target * ease(p)));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, active, duration, reducedMotion]);
  return value;
}

/* ------------------------------------------------------------------ */
/* Embedded CSS (keyframes / easing Tailwind's default build cannot   */
/* express without a JIT compiler)                                    */
/* ------------------------------------------------------------------ */

const EMBEDDED_CSS = `
  @keyframes nx-fade-up { from { opacity: 0; transform: translateY(6px) scale(0.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
  @keyframes nx-dash { to { stroke-dashoffset: -28; } }
  @keyframes nx-ring { 0% { transform: scale(0.9); opacity: 0.9; } 100% { transform: scale(2.1); opacity: 0; } }
  @keyframes nx-pop { from { opacity: 0; transform: scale(0.97); } to { opacity: 1; transform: scale(1); } }
  .nx-fade-up { animation: nx-fade-up 360ms ${EASE} both; }
  .nx-dash { animation: nx-dash 1.05s linear infinite; }
  .nx-ring { animation: nx-ring 1100ms ${EASE} infinite; }
  .nx-pop { animation: nx-pop 200ms ${EASE} both; }
  .nx-panel-enter { animation: nx-panel-in 260ms ${EASE} both; }
  @keyframes nx-panel-in { from { transform: translateX(16px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
  .nx-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
  .nx-scroll::-webkit-scrollbar-track { background: transparent; }
  .nx-scroll::-webkit-scrollbar-thumb { background: rgba(148,163,184,0.25); border-radius: 8px; }
  .nx-scroll::-webkit-scrollbar-thumb:hover { background: rgba(148,163,184,0.4); }
  .nx-canvas:active { cursor: grabbing; }
  button { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
  @media (prefers-reduced-motion: reduce) {
    .nx-fade-up, .nx-pop, .nx-panel-enter { animation-duration: 1ms !important; }
    .nx-dash, .nx-ring { animation: none !important; }
  }
`;

/* ------------------------------------------------------------------ */
/* Small UI primitives                                                */
/* ------------------------------------------------------------------ */

function IconButton({ icon: Icon, label, onClick, active, disabled, size = 15 }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={active}
      title={label}
      className={cx(
        "inline-flex h-8 w-8 items-center justify-center rounded-lg border transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70 focus-visible:ring-offset-2 focus-visible:ring-offset-slate-950",
        disabled && "cursor-not-allowed opacity-30",
        !disabled && active && "border-sky-400/50 bg-sky-400/15 text-sky-300",
        !disabled && !active && "border-slate-700/70 bg-slate-900/70 text-slate-400 hover:border-slate-600 hover:text-slate-200"
      )}
    >
      <Icon size={size} strokeWidth={2} aria-hidden="true" />
    </button>
  );
}

function SegButton({ active, onClick, icon: Icon, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cx(
        "relative inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70",
        active ? "bg-slate-100 text-slate-900" : "text-slate-400 hover:text-slate-200"
      )}
    >
      {Icon && <Icon size={13} strokeWidth={2.25} aria-hidden="true" />}
      {children}
    </button>
  );
}

function StatChip({ icon: Icon, value, label, active }) {
  const shown = useCountUp(value, active);
  return (
    <div className="flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/60 px-2.5 py-1.5">
      <Icon size={14} className="text-sky-300/80" strokeWidth={2} aria-hidden="true" />
      <div className="leading-none">
        <div className="font-mono text-sm font-semibold tabular-nums text-slate-100">{shown}</div>
        <div className="mt-0.5 text-slate-500" style={{ fontSize: 10 }}>{label}</div>
      </div>
    </div>
  );
}

function CopyableCode({ text }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);
  const onCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setCopied(false), 1400);
    } catch {
      // clipboard may be unavailable in this context; fail quietly
    }
  }, [text]);
  useEffect(() => () => clearTimeout(timerRef.current), []);
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-label={`Copy path ${text}`}
      className="group flex w-full items-center gap-2 rounded-md border border-slate-800 bg-slate-950/60 px-2.5 py-1.5 text-left font-mono text-xs text-sky-200/90 transition-colors hover:border-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
    >
      <FileCode2 size={13} className="shrink-0 text-slate-600" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate" translate="no">{text}</span>
      <span className="sr-only" aria-live="polite">{copied ? "Copied to clipboard" : ""}</span>
      {copied ? (
        <Check size={13} className="shrink-0 text-emerald-400" aria-hidden="true" />
      ) : (
        <Copy size={13} className="shrink-0 text-slate-600 opacity-0 transition-opacity group-hover:opacity-100" aria-hidden="true" />
      )}
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Graph: nodes and edges                                             */
/* ------------------------------------------------------------------ */

const NodeCard = memo(function NodeCard({ node, layer, isBuilt, focusState, dimmed, index, reducedMotion, onSelect, onHoverChange }) {
  const Icon = LAYER_ICON[node.layer] || Boxes;
  const emphasized = focusState === "selected" || focusState === "hovered";
  const codeLabel = (node.code && node.code[0]) ? node.code[0].split("/").slice(-1)[0] : "planned, not yet built";

  return (
    <g transform={`translate(${node.x - GLOW_PAD}, ${node.y - GLOW_PAD})`}>
      <foreignObject
        width={NODE_W + GLOW_PAD * 2}
        height={NODE_H + GLOW_PAD * 2}
        style={{ overflow: "visible" }}
      >
        <div
          style={{
            padding: GLOW_PAD,
            width: NODE_W + GLOW_PAD * 2,
            height: NODE_H + GLOW_PAD * 2,
            animationDelay: reducedMotion ? undefined : `${Math.min(index * 16, 480)}ms`,
          }}
          className={reducedMotion ? undefined : "nx-fade-up"}
        >
          <button
            type="button"
            aria-label={`${node.title}. ${layer.name} layer. ${isBuilt ? "Built." : "Planned, not yet built."}`}
            aria-pressed={focusState === "selected"}
            title={node.title}
            onClick={() => onSelect(node.id)}
            onMouseEnter={() => onHoverChange(node.id)}
            onMouseLeave={() => onHoverChange(null)}
            onFocus={() => onHoverChange(node.id)}
            onBlur={() => onHoverChange(null)}
            style={{
              width: NODE_W,
              height: NODE_H,
              borderColor: hexToRgba(layer.color, emphasized ? 0.9 : 0.38),
              borderStyle: isBuilt ? "solid" : "dashed",
              background: "rgba(9, 13, 26, 0.92)",
              boxShadow: emphasized
                ? `0 10px 28px -10px ${hexToRgba(layer.color, 0.5)}, 0 0 0 1px ${hexToRgba(layer.color, 0.18)}`
                : "0 1px 2px rgba(0,0,0,0.3)",
              transform: emphasized ? "translateY(-3px)" : "translateY(0)",
              opacity: dimmed ? 0.25 : 1,
              transitionProperty: "transform, box-shadow, border-color, opacity",
              transitionDuration: "200ms",
              transitionTimingFunction: EASE,
              font: "inherit",
              textAlign: "left",
            }}
            className="relative flex w-full cursor-pointer select-none flex-col justify-between rounded-xl border px-3.5 py-3 outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
          >
            <div className="flex items-start justify-between gap-2">
              <h3 className="min-w-0 flex-1 truncate text-sm font-semibold leading-snug text-slate-100">
                {node.title}
              </h3>
              {isBuilt ? (
                <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-slate-600" strokeWidth={2} aria-hidden="true" />
              ) : (
                <CircleDashed size={14} className="mt-0.5 shrink-0 text-slate-600" strokeWidth={2} aria-hidden="true" />
              )}
            </div>
            <div className="mt-1.5 flex items-center gap-1.5 text-xs" style={{ color: layer.color }}>
              <Icon size={11.5} strokeWidth={2.25} aria-hidden="true" />
              <span className="truncate">{layer.name}</span>
            </div>
            <div className="mt-2.5 truncate border-t border-slate-800/80 pt-2 font-mono text-sm text-slate-400" translate="no">
              {codeLabel}
            </div>
          </button>
        </div>
      </foreignObject>
    </g>
  );
});

const EdgePath = memo(function EdgePath({ edge, source, target, dimmed, highlighted, trustMode }) {
  const geo = useMemo(() => edgeGeometry(source, target), [source.x, source.y, target.x, target.y]);
  const isTrust = edge.kind === "trust";
  const isData = edge.kind === "data";
  const stroke = isTrust ? "#fb7185" : isData ? "#94a3b8" : "#5b6b8c";
  const showLabel = highlighted || (isTrust && trustMode);

  return (
    <g
      style={{
        opacity: dimmed ? 0.08 : isTrust ? (trustMode ? 1 : 0.55) : highlighted ? 1 : 0.65,
        transition: "opacity 220ms ease-out",
      }}
    >
      <path
        d={geo.d}
        fill="none"
        stroke={stroke}
        strokeWidth={highlighted ? 2.25 : isTrust ? 2 : 1.4}
        strokeDasharray={isTrust ? "7 5" : isData ? "1 4.5" : undefined}
        strokeLinecap="round"
        markerEnd={`url(#arrow-${edge.kind})`}
        className={isTrust && trustMode ? "nx-dash" : undefined}
      />
      {isData && <circle cx={geo.midX} cy={geo.midY} r={2.25} fill={stroke} opacity={0.85} />}
      {showLabel && (
        <g style={{ transition: "opacity 150ms ease-out" }}>
          <rect
            x={geo.midX - edge.label.length * 2.9 - 5}
            y={geo.midY - 17}
            width={edge.label.length * 5.8 + 10}
            height={15}
            rx={4}
            fill="rgba(2, 6, 23, 0.88)"
          />
          <text
            x={geo.midX}
            y={geo.midY - 6.5}
            textAnchor="middle"
            fill={isTrust ? "#fda4af" : "#cbd5e1"}
            fontSize={10}
            fontFamily="ui-monospace, monospace"
          >
            {edge.label}
          </text>
        </g>
      )}
    </g>
  );
});

function CornerBrackets() {
  const style = { borderColor: "rgba(100,116,139,0.35)" };
  return (
    <div className="pointer-events-none absolute inset-4 z-10">
      <div className="absolute left-0 top-0 h-3.5 w-3.5 border-l border-t" style={style} />
      <div className="absolute right-0 top-0 h-3.5 w-3.5 border-r border-t" style={style} />
      <div className="absolute bottom-0 left-0 h-3.5 w-3.5 border-b border-l" style={style} />
      <div className="absolute bottom-0 right-0 h-3.5 w-3.5 border-b border-r" style={style} />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Graph canvas (pan / zoom host)                                     */
/* ------------------------------------------------------------------ */

function computeBounds(nodes) {
  if (!nodes || nodes.length === 0) return { minX: 0, minY: 0, maxX: 1000, maxY: 800 };
  const minX = Math.min(...nodes.map((n) => n.x)) - 60;
  const minY = Math.min(...nodes.map((n) => n.y)) - 60;
  const maxX = Math.max(...nodes.map((n) => n.x + NODE_W)) + 60;
  const maxY = Math.max(...nodes.map((n) => n.y + NODE_H)) + 60;
  return { minX, minY, maxX, maxY };
}

function boundsToTransform(bounds, width, height) {
  const w = bounds.maxX - bounds.minX;
  const h = bounds.maxY - bounds.minY;
  const k = Math.min(1, Math.min(width / w, height / h) * 0.92);
  const tx = width / 2 - k * (bounds.minX + w / 2);
  const ty = height / 2 - k * (bounds.minY + h / 2);
  return d3.zoomIdentity.translate(tx, ty).scale(k);
}

function GraphCanvas({
  nodes, allNodesForBounds, edges, layersById, selectedId, hoveredId, dimSet, activeNeighborEdgeKeys,
  trustMode, onSelect, onHoverChange, reducedMotion, registerZoomApi,
}) {
  const svgRef = useRef(null);
  const gRef = useRef(null);
  const wrapRef = useRef(null);
  const zoomRef = useRef(null);
  const transformRef = useRef({ x: 0, y: 0, k: 1 });
  const [transform, setTransformState] = useState({ x: 0, y: 0, k: 1 });

  // Stable across filter changes (allNodesForBounds is a static, memoized full
  // node set), so the pan/zoom behavior is created once and never yanked out
  // from under the user just because they toggled a layer filter.
  const fullBounds = useMemo(() => computeBounds(allNodesForBounds), [allNodesForBounds]);
  const visibleBounds = useMemo(
    () => computeBounds(nodes.length ? nodes : allNodesForBounds),
    [nodes, allNodesForBounds]
  );

  useEffect(() => {
    const svgEl = svgRef.current;
    const wrapEl = wrapRef.current;
    if (!svgEl || !wrapEl) return;
    const svg = d3.select(svgEl);
    const zoom = d3
      .zoom()
      .scaleExtent([0.35, 2.2])
      .translateExtent([
        [fullBounds.minX - 400, fullBounds.minY - 400],
        [fullBounds.maxX + 400, fullBounds.maxY + 400],
      ])
      .on("zoom", (event) => {
        d3.select(gRef.current).attr("transform", event.transform.toString());
        const t = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
        transformRef.current = t;
        setTransformState(t);
      });
    svg.call(zoom).on("dblclick.zoom", null);
    zoomRef.current = zoom;

    const rect = wrapEl.getBoundingClientRect();
    svg.call(zoom.transform, boundsToTransform(fullBounds, rect.width, rect.height));

    return () => {
      svg.on(".zoom", null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fullBounds]);

  const runZoom = useCallback(
    (fn) => {
      if (!zoomRef.current || !svgRef.current) return;
      const svg = d3.select(svgRef.current);
      const sel = reducedMotion ? svg : svg.transition().duration(420).ease(d3.easeCubicOut);
      fn(sel, zoomRef.current);
    },
    [reducedMotion]
  );

  useEffect(() => {
    registerZoomApi({
      zoomIn: () => runZoom((sel, zoom) => sel.call(zoom.scaleBy, 1.35)),
      zoomOut: () => runZoom((sel, zoom) => sel.call(zoom.scaleBy, 1 / 1.35)),
      fit: () => {
        const rect = wrapRef.current.getBoundingClientRect();
        runZoom((sel, zoom) => sel.call(zoom.transform, boundsToTransform(visibleBounds, rect.width, rect.height)));
      },
      focusNode: (node) => {
        const rect = wrapRef.current.getBoundingClientRect();
        const k = Math.max(transformRef.current.k, 0.9);
        const tx = rect.width / 2 - k * (node.x + NODE_W / 2);
        const ty = rect.height / 2 - k * (node.y + NODE_H / 2);
        runZoom((sel, zoom) => sel.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k)));
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runZoom, visibleBounds]);

  const nodeById = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes]);
  const visibleEdgeSet = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);

  return (
    <div
      ref={wrapRef}
      role="region"
      aria-label="NomadicOS component graph, pannable and zoomable. Use Tab to move between modules."
      className="nx-canvas relative h-full w-full cursor-grab overflow-hidden"
      style={{
        backgroundColor: "#04070f",
        backgroundImage: "radial-gradient(circle, rgba(148,163,184,0.16) 1px, transparent 1px)",
        backgroundSize: "26px 26px",
        backgroundPosition: `${transform.x}px ${transform.y}px`,
      }}
    >
      <div
        className="pointer-events-none absolute inset-0 opacity-70"
        style={{ background: "radial-gradient(circle at 22% 12%, rgba(56,80,140,0.16), transparent 55%)" }}
      />
      <CornerBrackets />
      <svg ref={svgRef} className="h-full w-full touch-none" aria-hidden="false">
        <defs>
          <marker id="arrow-control" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#5b6b8c" />
          </marker>
          <marker id="arrow-data" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#94a3b8" />
          </marker>
          <marker id="arrow-trust" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="#fb7185" />
          </marker>
        </defs>
        <g ref={gRef}>
          {edges
            .filter((e) => visibleEdgeSet.has(e.from) && visibleEdgeSet.has(e.to))
            .map((e) => {
              const key = `${e.from}->${e.to}`;
              const source = nodeById[e.from];
              const target = nodeById[e.to];
              if (!source || !target) return null;
              const dimmed = dimSet ? dimSet.has(e.from) || dimSet.has(e.to) : false;
              return (
                <EdgePath
                  key={key}
                  edge={e}
                  source={source}
                  target={target}
                  dimmed={dimmed}
                  highlighted={activeNeighborEdgeKeys?.has(key)}
                  trustMode={trustMode}
                />
              );
            })}
          {nodes.map((n, i) => {
            const layer = layersById[n.layer];
            if (!layer) return null;
            const focusState = selectedId === n.id ? "selected" : hoveredId === n.id ? "hovered" : "idle";
            return (
              <NodeCard
                key={n.id}
                node={n}
                layer={layer}
                isBuilt={n.status !== "planned"}
                focusState={focusState}
                dimmed={dimSet ? dimSet.has(n.id) : false}
                index={i}
                reducedMotion={reducedMotion}
                onSelect={onSelect}
                onHoverChange={onHoverChange}
              />
            );
          })}
        </g>
      </svg>

      <div className="pointer-events-none absolute bottom-4 right-4 z-20 flex items-center gap-2">
        <div className="pointer-events-auto rounded-lg border border-slate-800 bg-slate-950/80 px-2 py-1 font-mono text-slate-500 backdrop-blur-sm tabular-nums" style={{ fontSize: 11 }}>
          {Math.round(transform.k * 100)}%
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Legend / filters (doubles as layer visibility toggle)              */
/* ------------------------------------------------------------------ */

function Legend({ layers, activeLayers, onToggleLayer, onResetLayers, counts }) {
  const allActive = activeLayers.size === layers.length;
  return (
    <div className="nx-scroll pointer-events-auto flex min-w-0 max-w-full items-center gap-1.5 overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/85 p-2 backdrop-blur-sm">
      <button
        type="button"
        onClick={onResetLayers}
        className={cx(
          "shrink-0 rounded-md px-2 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70",
          allActive ? "bg-slate-800 text-slate-200" : "text-slate-500 hover:text-slate-300"
        )}
      >
        All
      </button>
      {layers.map((l) => {
        const active = activeLayers.has(l.id);
        const Icon = LAYER_ICON[l.id] || Boxes;
        return (
          <button
            key={l.id}
            type="button"
            onClick={() => onToggleLayer(l.id)}
            aria-pressed={active}
            aria-label={`${l.name} layer, ${counts[l.id] || 0} modules`}
            title={`${l.name} (${counts[l.id] || 0})`}
            style={{
              borderColor: hexToRgba(l.color, active ? 0.55 : 0.18),
              color: active ? l.color : "#64748b",
              background: active ? hexToRgba(l.color, 0.1) : "transparent",
            }}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors duration-150 hover:bg-white/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
          >
            <Icon size={11.5} strokeWidth={2.25} aria-hidden="true" />
            <span className="hidden sm:inline">{l.name}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Detail panel                                                       */
/* ------------------------------------------------------------------ */

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "design", label: "How it\u2019s built" },
  { id: "verify", label: "Verification" },
];

function ConnectionChip({ dir, node, layer, label, onClick }) {
  const Icon = LAYER_ICON[node.layer] || Boxes;
  return (
    <button
      type="button"
      onClick={onClick}
      className="group flex w-full items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/50 px-2.5 py-2 text-left transition-colors hover:border-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
    >
      <span
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md"
        style={{ backgroundColor: hexToRgba(layer.color, 0.14), color: layer.color }}
      >
        <Icon size={12.5} strokeWidth={2.25} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium text-slate-200">{node.title}</span>
        <span className="block truncate font-mono text-slate-500" style={{ fontSize: 10.5 }} translate="no">
          {dir === "out" ? "\u2192" : "\u2190"} {label}
        </span>
      </span>
      <ArrowRight size={13} className="shrink-0 text-slate-700 transition-transform group-hover:translate-x-0.5 group-hover:text-slate-400" aria-hidden="true" />
    </button>
  );
}

function DetailPanel({ node, layer, edges, nodesById, layersById, onClose, onFocusNode, isMobile }) {
  const [tab, setTab] = useState("overview");
  const panelRef = useRef(null);
  const closeBtnRef = useRef(null);
  const previouslyFocused = useRef(null);

  useEffect(() => {
    setTab("overview");
  }, [node?.id]);

  useEffect(() => {
    if (!node) return;
    previouslyFocused.current = document.activeElement;
    closeBtnRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previouslyFocused.current && previouslyFocused.current.focus) {
        previouslyFocused.current.focus();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.id]);

  if (!node) return null;
  const isBuilt = node.status !== "planned";

  const connections = edges
    .filter((e) => e.from === node.id || e.to === node.id)
    .map((e) => {
      const outgoing = e.from === node.id;
      const otherId = outgoing ? e.to : e.from;
      const other = nodesById[otherId];
      return other ? { dir: outgoing ? "out" : "in", node: other, label: e.label, kind: e.kind } : null;
    })
    .filter(Boolean);

  return (
    <>
      {isMobile && (
        <div
          className="fixed inset-0 z-30 bg-slate-950/70 backdrop-blur-sm"
          onClick={onClose}
          aria-hidden="true"
        />
      )}
      <aside
        ref={panelRef}
        role="dialog"
        aria-modal={isMobile ? "true" : undefined}
        aria-label={`${node.title} details`}
        className={cx(
          "nx-panel-enter z-40 flex flex-col overflow-hidden border-slate-800 bg-slate-950",
          isMobile ? "fixed inset-x-0 bottom-0 top-14 rounded-t-2xl border-t" : "relative h-full w-96 border-l"
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-slate-800 px-5 pb-4 pt-5">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-xs font-medium" style={{ color: layer.color }}>
              {(() => {
                const Icon = LAYER_ICON[node.layer] || Boxes;
                return <Icon size={12.5} strokeWidth={2.25} />;
              })()}
              {layer.name}
              <span className="text-slate-700">&middot;</span>
              {isBuilt ? (
                <span className="inline-flex items-center gap-1 text-slate-400">
                  <CheckCircle2 size={12} aria-hidden="true" /> Built
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 text-slate-500">
                  <CircleDashed size={12} aria-hidden="true" /> Planned
                </span>
              )}
            </div>
            <h2 className="mt-1 text-balance text-lg font-semibold leading-snug text-slate-50">{node.title}</h2>
          </div>
          <button
            ref={closeBtnRef}
            type="button"
            onClick={onClose}
            aria-label="Close panel"
            className="mt-0.5 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-800 text-slate-400 transition-colors hover:border-slate-700 hover:text-slate-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="flex flex-wrap gap-1.5 border-b border-slate-800 px-5 py-3">
          {node.bp.map((b) => (
            <span key={b} className="rounded-md border border-slate-800 bg-slate-900/70 px-1.5 py-0.5 font-mono text-sky-300/90" style={{ fontSize: 10.5 }} translate="no">
              {b}
            </span>
          ))}
        </div>

        <div role="tablist" aria-label="Node detail sections" className="flex gap-1 border-b border-slate-800 px-3 pt-2">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={(e) => {
                if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                  const idx = TABS.findIndex((x) => x.id === tab);
                  const next = e.key === "ArrowRight" ? (idx + 1) % TABS.length : (idx - 1 + TABS.length) % TABS.length;
                  setTab(TABS[next].id);
                }
              }}
              className={cx(
                "relative rounded-t-md px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70",
                tab === t.id ? "text-slate-50" : "text-slate-500 hover:text-slate-300"
              )}
            >
              {t.label}
              {tab === t.id && <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-sky-400" />}
            </button>
          ))}
        </div>

        <div className="nx-scroll flex-1 overflow-y-auto overscroll-contain px-5 py-4">
          {tab === "overview" && (
            <div key="overview" className="nx-pop space-y-5">
              <p className="text-sm leading-relaxed text-slate-300">{node.summary}</p>
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">What it does</h3>
                <ul className="space-y-1.5">
                  {node.what.map((w, i) => (
                    <li key={i} className="flex gap-2 text-sm leading-relaxed text-slate-300">
                      <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-600" />
                      {w}
                    </li>
                  ))}
                </ul>
              </section>
              {connections.length > 0 && (
                <section>
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    <Link2 size={12} aria-hidden="true" /> Connections
                  </h3>
                  <div className="space-y-1.5">
                    {connections.map((c, i) => (
                      <ConnectionChip
                        key={i}
                        dir={c.dir}
                        node={c.node}
                        layer={layersById[c.node.layer]}
                        label={c.label}
                        onClick={() => onFocusNode(c.node.id)}
                      />
                    ))}
                  </div>
                </section>
              )}
            </div>
          )}

          {tab === "design" && (
            <div key="design" className="nx-pop space-y-5">
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">How it works</h3>
                <ul className="space-y-1.5">
                  {node.how.map((w, i) => (
                    <li key={i} className="flex gap-2 text-sm leading-relaxed text-slate-300">
                      <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-slate-600" />
                      {w}
                    </li>
                  ))}
                </ul>
              </section>
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Technology &amp; rationale</h3>
                <div className="space-y-2">
                  {node.tech.map((t, i) => (
                    <div key={i} className="rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2">
                      <div className="font-mono text-xs font-semibold text-sky-300">{t.name}</div>
                      <div className="mt-0.5 text-xs leading-relaxed text-slate-400">{t.why}</div>
                    </div>
                  ))}
                </div>
              </section>
              <section className="rounded-lg border border-violet-500/20 bg-violet-500/5 px-3 py-2.5">
                <h3 className="mb-1 text-xs font-semibold text-violet-300">Why this design</h3>
                <p className="text-xs leading-relaxed text-slate-300">{node.why}</p>
              </section>
            </div>
          )}

          {tab === "verify" && (
            <div key="verify" className="nx-pop space-y-5">
              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  <ShieldCheck size={12} aria-hidden="true" /> Security invariants guarded
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {node.invariants.map((inv) => (
                    <span
                      key={inv}
                      className="rounded-md border border-rose-500/25 bg-rose-500/10 px-2 py-0.5 text-xs text-rose-300"
                    >
                      {inv}
                    </span>
                  ))}
                </div>
              </section>
              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  <FileCode2 size={12} aria-hidden="true" /> Code
                </h3>
                {node.code && node.code.length > 0 ? (
                  <div className="space-y-1.5">
                    {node.code.map((c) => (
                      <CopyableCode key={c} text={c} />
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-slate-500">Not written yet.</p>
                )}
              </section>
              <section>
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
                  <FlaskConical size={12} aria-hidden="true" /> Tests
                </h3>
                {node.tests && node.tests.length > 0 ? (
                  <div className="space-y-1.5">
                    {node.tests.map((c) => (
                      <CopyableCode key={c} text={c} />
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-slate-500">No tests yet.</p>
                )}
              </section>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Flow (task lifecycle) view                                         */
/* ------------------------------------------------------------------ */

function StepCard({ step, index, total, active, layer, onActivate, stepRef, reducedMotion }) {
  const Icon = LAYER_ICON[step.layer] || Boxes;
  const numberMatch = step.title.match(/^(\d+)\s*[·.]\s*(.*)$/);
  const displayTitle = numberMatch ? numberMatch[2] : step.title;

  return (
    <div ref={stepRef} className="relative flex gap-4">
      <div className="flex w-8 shrink-0 flex-col items-center">
        <button
          type="button"
          onClick={onActivate}
          aria-label={`Step ${index + 1}: ${displayTitle}`}
          aria-current={active}
          style={{
            borderColor: active ? layer.color : "rgba(51,64,110,0.8)",
            color: active ? layer.color : "#64748b",
            boxShadow: active ? `0 0 0 4px ${hexToRgba(layer.color, 0.14)}` : "none",
          }}
          className="z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border-2 bg-slate-950 font-mono text-xs font-semibold transition duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
        >
          {index + 1}
        </button>
        {index < total - 1 && <div className="mt-1 w-px flex-1 bg-slate-800" />}
      </div>

      <button
        type="button"
        onClick={onActivate}
        className={cx(
          "mb-5 flex-1 rounded-xl border px-4 py-3.5 text-left transition duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70",
          active ? "border-slate-700 bg-slate-900/70" : "border-slate-800/70 bg-slate-950/40 hover:border-slate-800 hover:bg-slate-900/40"
        )}
      >
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold text-slate-100">{displayTitle}</h3>
          <span className="flex shrink-0 items-center gap-1 text-xs" style={{ color: layer.color }}>
            <Icon size={11.5} strokeWidth={2.25} />
            {layer.name}
          </span>
        </div>
        <p className="mt-1.5 text-sm leading-relaxed text-slate-400">{step.what}</p>

        <div
          style={{
            display: "grid",
            gridTemplateRows: active ? "1fr" : "0fr",
            transition: reducedMotion ? "none" : `grid-template-rows 240ms ${EASE}`,
          }}
        >
          <div className="overflow-hidden">
            <div className="mt-3 space-y-3 border-t border-slate-800 pt-3">
              <div>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">How</div>
                <p className="text-xs leading-relaxed text-slate-400">{step.how}</p>
              </div>
              <div>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Technology</div>
                <p className="font-mono text-xs leading-relaxed text-sky-300/90">{step.tech}</p>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                {step.bp.map((b) => (
                  <span key={b} className="rounded-md border border-slate-800 bg-slate-900/70 px-1.5 py-0.5 font-mono text-slate-400" style={{ fontSize: 10.5 }} translate="no">
                    {b}
                  </span>
                ))}
              </div>
              <div className="font-mono text-slate-600" style={{ fontSize: 10.5 }} translate="no">{step.code}</div>
            </div>
          </div>
        </div>
      </button>
    </div>
  );
}

function FlowView({ flow, layersById, reducedMotion }) {
  const background = useMemo(() => flow.filter((s) => !/^\d/.test(s.title.trim())), [flow]);
  const sequence = useMemo(() => flow.filter((s) => /^\d/.test(s.title.trim())), [flow]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const stepRefs = useRef([]);
  const listRef = useRef(null);

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      setActiveIndex((i) => (i + 1) % sequence.length);
    }, 2800);
    return () => clearInterval(id);
  }, [playing, sequence.length]);

  useEffect(() => {
    const el = stepRefs.current[activeIndex];
    if (el && listRef.current) {
      el.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
    }
  }, [activeIndex, reducedMotion]);

  const activate = (i) => {
    setPlaying(false);
    setActiveIndex(i);
  };

  return (
    <div ref={listRef} className="nx-scroll h-full overflow-y-auto overscroll-contain px-5 py-6 md:px-10 lg:px-16">
      <div className="mx-auto max-w-2xl">
        <h2 className="text-lg font-semibold text-slate-50">Task lifecycle</h2>
        <p className="mt-1.5 max-w-xl text-sm leading-relaxed text-slate-400">
          Every goal travels this loop: observe, propose, mediate, execute, verify. Each step is mediated,
          budgeted, verified and audited, and the model proposes but never executes directly.
        </p>

        {background.length > 0 && (
          <div className="mt-5 space-y-2">
            {background.map((s) => {
              const layer = layersById[s.layer];
              const Icon = LAYER_ICON[s.layer] || Boxes;
              return (
                <div key={s.id} className="flex items-start gap-3 rounded-xl border border-dashed border-slate-800 bg-slate-900/30 px-4 py-3">
                  <span
                    className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md"
                    style={{ backgroundColor: hexToRgba(layer.color, 0.12), color: layer.color }}
                  >
                    <RefreshCw size={12} aria-hidden="true" />
                  </span>
                  <div>
                    <div className="flex items-center gap-2 text-sm font-medium text-slate-200">
                      {s.title}
                      <span className="rounded border border-slate-700 px-1.5 py-0.5 font-normal text-slate-500" style={{ fontSize: 10 }}>
                        background
                      </span>
                    </div>
                    <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{s.what}</p>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="mt-7 flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/50 px-4 py-2.5">
          <div className="flex items-center gap-1.5">
            <IconButton icon={SkipBack} label="Previous step" onClick={() => activate((activeIndex - 1 + sequence.length) % sequence.length)} />
            <IconButton icon={playing ? Pause : Play} label={playing ? "Pause" : "Play"} onClick={() => setPlaying((p) => !p)} active={playing} />
            <IconButton icon={SkipForward} label="Next step" onClick={() => activate((activeIndex + 1) % sequence.length)} />
          </div>
          <div className="font-mono text-xs text-slate-500">
            Step {activeIndex + 1} of {sequence.length}
          </div>
        </div>

        <div className="mt-7">
          {sequence.map((s, i) => (
            <StepCard
              key={s.id}
              step={s}
              index={i}
              total={sequence.length}
              active={i === activeIndex}
              layer={layersById[s.layer]}
              onActivate={() => activate(i)}
              stepRef={(el) => (stepRefs.current[i] = el)}
              reducedMotion={reducedMotion}
            />
          ))}
        </div>

        <div className="flex items-center gap-3 rounded-xl border border-rose-500/20 bg-rose-500/5 px-4 py-3 text-xs text-rose-200/90">
          <RefreshCw size={14} className="shrink-0 text-rose-400" aria-hidden="true" />
          Repeats until the goal is met or a budget stops it, with the security gate mediating every step along
          the way.
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* App                                                                 */
/* ------------------------------------------------------------------ */

export default function ArchitectureExplorer() {
  const data = ARCHITECTURE_DATA;
  const reducedMotion = useReducedMotion();
  const isMobile = useMediaQuery("(max-width: 767px)");

  const layers = data.layers;
  const layersById = useMemo(() => Object.fromEntries(layers.map((l) => [l.id, l])), [layers]);
  const allNodes = useMemo(() => layoutWithFallback(data.nodes, layers), [data.nodes, layers]);
  const nodesById = useMemo(() => Object.fromEntries(allNodes.map((n) => [n.id, n])), [allNodes]);
  const edges = data.edges;

  const [viewMode, setViewMode] = useState("graph");
  const [selectedId, setSelectedId] = useState(null);
  const [hoveredId, setHoveredId] = useState(null);
  const [query, setQuery] = useState("");
  const [activeLayers, setActiveLayers] = useState(() => new Set(layers.map((l) => l.id)));
  const [statusFilter, setStatusFilter] = useState("all");
  const [trustMode, setTrustMode] = useState(false);
  const zoomApiRef = useRef(null);
  const searchRef = useRef(null);

  const registerZoomApi = useCallback((api) => {
    zoomApiRef.current = api;
  }, []);

  const layerCounts = useMemo(() => {
    const c = {};
    allNodes.forEach((n) => (c[n.layer] = (c[n.layer] || 0) + 1));
    return c;
  }, [allNodes]);

  const q = query.trim().toLowerCase();
  const searchMatchIds = useMemo(() => {
    if (!q) return null;
    return new Set(
      allNodes
        .filter(
          (n) =>
            n.title.toLowerCase().includes(q) ||
            n.id.toLowerCase().includes(q) ||
            n.summary.toLowerCase().includes(q) ||
            n.tech.some((t) => t.name.toLowerCase().includes(q)) ||
            (n.code || []).some((c) => c.toLowerCase().includes(q))
        )
        .map((n) => n.id)
    );
  }, [q, allNodes]);

  const visibleNodes = useMemo(
    () =>
      allNodes.filter((n) => {
        if (!activeLayers.has(n.layer)) return false;
        if (statusFilter === "built" && n.status === "planned") return false;
        if (statusFilter === "planned" && n.status !== "planned") return false;
        return true;
      }),
    [allNodes, activeLayers, statusFilter]
  );

  const dimSet = useMemo(() => {
    if (searchMatchIds) {
      const s = new Set(visibleNodes.filter((n) => !searchMatchIds.has(n.id)).map((n) => n.id));
      return s;
    }
    const activeId = selectedId || hoveredId;
    if (!activeId || !nodesById[activeId]) return null;
    const neighbors = new Set([activeId]);
    edges.forEach((e) => {
      if (e.from === activeId) neighbors.add(e.to);
      if (e.to === activeId) neighbors.add(e.from);
    });
    return new Set(visibleNodes.filter((n) => !neighbors.has(n.id)).map((n) => n.id));
  }, [searchMatchIds, selectedId, hoveredId, visibleNodes, edges, nodesById]);

  const activeNeighborEdgeKeys = useMemo(() => {
    const activeId = selectedId || hoveredId;
    if (!activeId) return new Set();
    return new Set(
      edges.filter((e) => e.from === activeId || e.to === activeId).map((e) => `${e.from}->${e.to}`)
    );
  }, [selectedId, hoveredId, edges]);

  const focusNode = useCallback(
    (id) => {
      const node = nodesById[id];
      if (!node) return;
      setSelectedId(id);
      setViewMode("graph");
      // Make sure the target is actually visible under the current filters,
      // otherwise we would zoom to an empty spot where a hidden node lives.
      setActiveLayers((prev) => (prev.has(node.layer) ? prev : new Set([...prev, node.layer])));
      setStatusFilter((prev) => {
        const wouldHide = (prev === "built" && node.status === "planned") || (prev === "planned" && node.status !== "planned");
        return wouldHide ? "all" : prev;
      });
      zoomApiRef.current?.focusNode(node);
    },
    [nodesById]
  );

  const toggleLayer = (id) => {
    setActiveLayers((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next.size === 0 ? new Set(layers.map((l) => l.id)) : next;
    });
  };

  useEffect(() => {
    const onKey = (e) => {
      const tag = document.activeElement?.tagName;
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (e.key === "/" && !typing) {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "Escape") {
        if (document.activeElement === searchRef.current) {
          setQuery("");
          searchRef.current?.blur();
        } else if (selectedId) {
          setSelectedId(null);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId]);

  useEffect(() => {
    if (q && searchMatchIds && searchMatchIds.size > 0) {
      const first = allNodes.find((n) => searchMatchIds.has(n.id));
      if (first) requestAnimationFrame(() => zoomApiRef.current?.focusNode(first));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const selectedNode = selectedId ? nodesById[selectedId] : null;
  const stats = data.meta.stats;

  return (
    <div
      className="relative flex h-full w-full flex-col overflow-hidden bg-slate-950 font-sans text-slate-200"
      style={{ minHeight: 640, colorScheme: "dark" }}
    >
      <style>{EMBEDDED_CSS}</style>
      <a
        href="#nx-main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-sky-400 focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:text-slate-950"
      >
        Skip to explorer
      </a>

      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3.5">
        <div className="flex items-baseline gap-3">
          <h1 className="font-mono text-base font-semibold tracking-tight text-slate-50">
            {data.meta.project}
          </h1>
          <span className="hidden text-xs text-slate-500 sm:inline">{data.meta.subtitle}</span>
        </div>
        <div className="flex items-center gap-2">
          <StatChip icon={Boxes} value={stats.modules} label="Modules" active />
          <StatChip icon={FlaskConical} value={stats.tests} label="Tests" active />
          <StatChip icon={ShieldCheck} value={stats.adrs} label="ADRs" active />
          <StatChip icon={Waypoints} value={stats.phases} label="Phases" active />
        </div>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 bg-slate-950/60 px-5 py-2.5">
        <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-900/60 p-1">
          <SegButton active={viewMode === "graph"} onClick={() => setViewMode("graph")} icon={Waypoints}>
            Graph
          </SegButton>
          <SegButton
            active={viewMode === "flow"}
            onClick={() => {
              setViewMode("flow");
              setSelectedId(null);
            }}
            icon={Workflow}
          >
            Lifecycle
          </SegButton>
        </div>

        {viewMode === "graph" && (
          <>
            <div className="relative flex-1" style={{ minWidth: 160 }}>
              <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" aria-hidden="true" />
              <input
                ref={searchRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search modules, tech, code paths…"
                aria-label="Search modules, technology, or code paths"
                autoComplete="off"
                spellCheck={false}
                className="w-full rounded-lg border border-slate-800 bg-slate-900/60 py-1.5 pl-8 pr-8 text-xs text-slate-200 placeholder:text-slate-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400/70"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  aria-label="Clear search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
                >
                  <X size={13} aria-hidden="true" />
                </button>
              )}
            </div>

            <div className="flex items-center gap-1 rounded-lg border border-slate-800 bg-slate-900/60 p-1">
              <SegButton active={statusFilter === "all"} onClick={() => setStatusFilter("all")}>All</SegButton>
              <SegButton active={statusFilter === "built"} onClick={() => setStatusFilter("built")}>Built</SegButton>
              <SegButton active={statusFilter === "planned"} onClick={() => setStatusFilter("planned")}>Planned</SegButton>
            </div>

            <IconButton
              icon={trustMode ? ShieldAlert : ShieldCheck}
              label={trustMode ? "Hide trust boundary" : "Show trust boundary"}
              onClick={() => setTrustMode((v) => !v)}
              active={trustMode}
            />
          </>
        )}
      </div>

      <div className="relative flex flex-1 overflow-hidden">
        <main id="nx-main" tabIndex={-1} className="relative flex-1 overflow-hidden outline-none">
          {viewMode === "graph" ? (
            <>
              <GraphCanvas
                nodes={visibleNodes}
                allNodesForBounds={allNodes}
                edges={edges}
                layersById={layersById}
                selectedId={selectedId}
                hoveredId={hoveredId}
                dimSet={dimSet}
                activeNeighborEdgeKeys={activeNeighborEdgeKeys}
                trustMode={trustMode}
                onSelect={focusNode}
                onHoverChange={setHoveredId}
                reducedMotion={reducedMotion}
                registerZoomApi={registerZoomApi}
              />
              <div className="pointer-events-none absolute inset-x-0 bottom-4 z-20 flex items-end justify-between px-4">
                <Legend
                  layers={layers}
                  activeLayers={activeLayers}
                  onToggleLayer={toggleLayer}
                  onResetLayers={() => setActiveLayers(new Set(layers.map((l) => l.id)))}
                  counts={layerCounts}
                />
                <div className="pointer-events-auto flex flex-col gap-1.5 rounded-xl border border-slate-800 bg-slate-950/85 p-1.5 backdrop-blur-sm">
                  <IconButton icon={ZoomIn} label="Zoom in" onClick={() => zoomApiRef.current?.zoomIn()} />
                  <IconButton icon={ZoomOut} label="Zoom out" onClick={() => zoomApiRef.current?.zoomOut()} />
                  <IconButton icon={Maximize2} label="Fit to view" onClick={() => zoomApiRef.current?.fit()} />
                </div>
              </div>
              {visibleNodes.length === 0 && (
                <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                  <div className="pointer-events-auto rounded-xl border border-slate-800 bg-slate-950/90 px-5 py-4 text-center">
                    <p className="text-sm text-slate-300">No modules match the current filters.</p>
                    <button
                      type="button"
                      onClick={() => {
                        setActiveLayers(new Set(layers.map((l) => l.id)));
                        setStatusFilter("all");
                      }}
                      className="mt-2 text-xs font-medium text-sky-300 hover:text-sky-200"
                    >
                      Reset filters
                    </button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <FlowView flow={data.flow} layersById={layersById} reducedMotion={reducedMotion} />
          )}
        </main>

        {selectedNode && (
          <DetailPanel
            node={selectedNode}
            layer={layersById[selectedNode.layer]}
            edges={edges}
            nodesById={nodesById}
            layersById={layersById}
            onClose={() => setSelectedId(null)}
            onFocusNode={focusNode}
            isMobile={isMobile}
          />
        )}
      </div>
    </div>
  );
}
