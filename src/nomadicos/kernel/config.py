"""Configuration selects bricks (SPEC §42), validated at load time.

Loading must fail closed on invalid configuration: a typo in an engine name
must never silently produce a half-wired system.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nomadicos.kernel.errors import ConfigInvalid

EngineName = Literal["llamacpp", "ollama", "mock"]
CapabilityPattern = str  # e.g. "filesystem.*", "terminal.execute", "git.*"
RoutingMode = Literal["FAST", "BALANCED", "QUALITY"]

#: Capability namespaces that may appear in an autonomy profile. Validated
#: against this set so configuration cannot invent authority namespaces.
KNOWN_CAPABILITY_NAMESPACES = frozenset(
    {
        "filesystem",
        "terminal",
        "desktop",
        "keyboard",
        "mouse",
        "browser",
        "application",
        "process",
        "network",
        "service",
        "git",
        "memory",
    }
)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InferenceConfig(_Contract):
    # Engine priority (owner directive 2026-09-15): llama.cpp #1, Ollama #2.
    default_engine: EngineName = "llamacpp"
    fallback_engine: EngineName = "ollama"
    ollama_base_url: str = "http://localhost:11434"
    llama_server_url: str = "http://127.0.0.1:8080"
    llama_server_bin: str = "llama-server"
    llama_port: int = Field(default=8080, ge=1, le=65535)
    models_dir: str = "models"
    request_timeout_s: float = Field(default=600.0, gt=0)


class ModelRoles(_Contract):
    """Configurable model names — never hard-coded into components (SPEC §11)."""

    tiny: str | None = None
    worker: str | None = None
    coding_worker: str | None = None
    reasoning: str | None = None
    coding_escalation: str | None = None
    critic: str | None = None


class AutonomyConfig(_Contract):
    #: Descriptive posture name. The committed/default posture is
    #: least-privilege: nothing is authorized until the owner runs an
    #: explicit grant (SPEC §5), and even then only actions inside the
    #: configured capability ceiling are allowed. Deliberately enabling
    #: broader autonomy requires BOTH listing the capability patterns here
    #: (e.g. ``FULL_PC_AUTONOMY`` with terminal/git/process/desktop) AND the
    #: owner grant. This field is descriptive; enforcement lives entirely in
    #: CapabilityPolicy over these capability patterns + owner state.
    profile: str = "LIMITED"
    #: Least-privilege default ceiling: filesystem actions only. Absent or
    #: unavailable configuration can never silently restore a broader
    #: ceiling — a fresh install without YAML gets exactly this set, and
    #: every action still requires the explicit owner grant.
    capabilities: list[CapabilityPattern] = Field(default_factory=lambda: ["filesystem.*"])
    denied_resources: list[str] = Field(default_factory=list)

    @field_validator("capabilities")
    @classmethod
    def _known_namespaces(cls, v: list[str]) -> list[str]:
        for cap in v:
            ns = cap.split(".", 1)[0]
            if ns not in KNOWN_CAPABILITY_NAMESPACES:
                raise ValueError(f"unknown capability namespace in {cap!r}")
        return v

    @model_validator(mode="after")
    def _unique_denied(self) -> AutonomyConfig:
        if len(self.denied_resources) != len(set(self.denied_resources)):
            raise ValueError("denied_resources must be unique")
        return self


class VerificationConfig(_Contract):
    require_goal_proof: bool = True

    @model_validator(mode="after")
    def _no_unverified_success(self) -> VerificationConfig:
        if not self.require_goal_proof:
            raise ValueError(
                "goal proof is a hard invariant (SPEC §28, §56.10) and cannot be disabled"
            )
        return self


class BudgetConfig(_Contract):
    """Bounded retries/escalation/recovery (SPEC §14, §30) with defaults."""

    max_plan_steps: int = Field(default=24, ge=1, le=200)
    max_attempts_per_step: int = Field(default=3, ge=1, le=10)
    max_recoveries: int = Field(default=3, ge=1, le=10)
    max_escalations: int = Field(default=2, ge=0, le=6)
    max_critic_iterations: int = Field(default=4, ge=1, le=12)
    max_total_steps: int = Field(default=120, ge=4, le=2000)
    stuck_repeat_threshold: int = Field(default=2, ge=2, le=8)
    escalation_repeat_threshold: int = Field(default=2, ge=1, le=8)
    action_timeout_s: float = Field(default=120.0, gt=0, le=3600)
    generation_timeout_s: float = Field(default=300.0, gt=0, le=3600)
    # ---- Phase 4C worker concurrency / resource limits (trusted config;
    # workers can never change these) ----
    #: max specialist workers running at once. 1 = fully sequential (the
    #: pre-4C behavior). >1 enables CONCURRENT READ-ONLY (research/testing)
    #: worker batches only; side-effecting tasks (coding/EXECUTE) always run
    #: sequentially through the harness.
    max_concurrent_workers: int = Field(default=1, ge=1, le=8)
    #: total specialist worker launches allowed per mission; exceeding it is
    #: a typed budget failure, never a silent skip
    max_workers_per_mission: int = Field(default=16, ge=1, le=64)
    #: enforced wall-clock budget around each worker execution (real timeout,
    #: not a suggestion)
    worker_timeout_s: float = Field(default=60.0, gt=0, le=3600)
    #: grace a timed-out/cancelled worker gets to die before its outcome is
    #: declared OUTCOME_UNKNOWN (uncertain, never auto-replayed)
    worker_cancel_grace_s: float = Field(default=5.0, gt=0, le=60)
    #: bounded worker output/diagnostics retained per result
    max_worker_output_chars: int = Field(default=20_000, ge=100, le=1_000_000)
    #: bounded worker_results retained per mission (older entries dropped)
    max_retained_worker_results: int = Field(default=64, ge=1, le=512)

    # ---- Phase 4D adaptive decision / replanning budgets (trusted config;
    # models can never change these) ----
    #: max adaptive decision cycles per mission; exceeding it is a typed
    #: budget failure, never an infinite loop
    max_adaptive_decisions_per_mission: int = Field(default=10, ge=1, le=100)
    #: max retries per individual task; retries beyond this force REPLAN or
    #: STOP (never unbounded re-execution of a failing task)
    max_retries_per_task: int = Field(default=3, ge=1, le=20)
    #: max plan revisions per mission; replans beyond this force ESCALATE or
    #: STOP
    max_replans_per_mission: int = Field(default=3, ge=0, le=20)
    #: max model/reasoning calls per adaptive decision cycle; prevents
    #: unbounded model spinning when the Brain cannot resolve a situation
    max_reasoning_calls_per_cycle: int = Field(default=2, ge=1, le=10)


class RouterConfig(_Contract):
    mode: RoutingMode = "BALANCED"
    minimum_quality: dict[str, float] = Field(
        default_factory=lambda: {"FAST": 0.55, "BALANCED": 0.7, "QUALITY": 0.85}
    )
    historical_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    #: minimum measured samples before the quality gate may reject a
    #: candidate (Phase 14C-B): fewer samples are insufficient evidence for
    #: the gate - a single failed sample can no longer exclude a model;
    #: N=1 reproduces the previous always-gate behavior
    minimum_sample_count: int = Field(default=3, ge=1)
    #: freshness policy for stored metric evidence (Phase 14C-C): records
    #: older than this TTL are excluded from aggregation, so expired
    #: evidence can never dominate fresh evidence; None = disabled (all
    #: evidence eligible - the previous behavior)
    metric_ttl_s: float | None = Field(default=None, gt=0)
    #: KNOWN host resource budgets (Phase 14C-G): a candidate whose
    #: declared vram_bytes/ram_bytes exceeds the budget is excluded; either
    #: side unknown = no constraint (deterministic cold-start fallback)
    max_vram_bytes: int | None = Field(default=None, gt=0)
    max_ram_bytes: int | None = Field(default=None, gt=0)
    #: host context-window budget (Phase 14D): a candidate whose advertised
    #: context length exceeds this is excluded. Either side unknown = no
    #: constraint. Advertised context length is NOT the same as the runtime's
    #: configured context setting (§14D-1 correction).
    max_context_bytes: int | None = Field(default=None, gt=0)
    #: ---- quality-constrained routing (Phase 14D) ----
    #: minimum verified-success probability per task bucket. A candidate
    #: whose conservative reliability estimate falls below the bucket floor is
    #: excluded from the quality path even when it is fast. Chat-shaped work
    #: has no floor. The floor only bites once enough benchmark evidence
    #: exists (see ``quality_min_samples``); cold start is never gated.
    quality_min: dict[str, float] = Field(
        default_factory=lambda: {
            "coding": 0.80,
            "coding+reasoning": 0.80,
            "testing": 0.80,
            "terminal": 0.75,
            "simple_file": 0.75,
            "reasoning": 0.80,
            "planning": 0.75,
            "classification": 0.0,
            "chat": 0.0,
        }
    )
    #: minimum measured samples before the quality floor may bite. Below this
    #: the candidate stays in the quality pool at its conservative estimate —
    #: the floor is a benchmark-derived policy, not a cold-start gate. The
    #: Wilson lower bound is conservative enough that a small sample almost
    #: never clears a 0.80 floor, so the quality path naturally falls back to
    #: the deterministic size-based policy until real benchmark evidence
    #: accumulates.
    quality_min_samples: int = Field(default=5, ge=1)
    #: utility weights: eta = reliability, alpha = latency, beta = memory,
    #: gamma = monetary cost (omitted for local-first configs)
    utility_weights: dict[str, float] = Field(
        default_factory=lambda: {"eta": 1.0, "alpha": 1.0, "beta": 0.5, "gamma": 0.0}
    )
    #: normalisation references: L_ref in ms, R_ref in MiB
    latency_ref_ms: float = Field(default=30_000.0, gt=0)
    memory_ref_mib: float = Field(default=8_192.0, gt=0)
    #: per-model repeat cap inside an escalation chain (Phase 14D-3): a model
    #: that repeatedly times out does not keep getting attempts just because its
    #: historical quality score is high
    escalation_repeat_cap: int = Field(default=2, ge=1, le=10)
    #: conservative prior used for unmeasured candidates (never presented as
    #: measured reliability)
    unmeasured_p_lcb: float = Field(default=0.5, ge=0.0, le=1.0)


class OrchestrationConfig(_Contract):
    engine: Literal["langgraph"] = "langgraph"


# ---------------------------------------------------------------------------
# Environment loading for deployment configuration (SPEC §42).
#
# The root `.env` provides deployment-level settings (hosts, ports, secrets)
# without committing them. Process environment variables always win over
# `.env` (os.environ.setdefault), so an explicit `POSTGRES_PASSWORD=...` in
# the shell overrides the file. Credentials are never logged.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Repository root, resolved from this file's location -- never CWD."""
    return Path(__file__).resolve().parents[3]  # src/nomadicos/kernel/config.py


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Load a root ``.env`` into a local dict (without mutating ``os.environ``).

    Process environment variables (read separately) take precedence over these
    values. Lines are ``KEY=VALUE``; surrounding single/double quotes are
    stripped. Blank lines and ``#`` comments are ignored. Missing file is
    silently fine.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _quote_conninfo_value(value: str) -> str:
    """Quote a libpq connection-info value, escaping backslash and quote."""
    simple = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
    if all(c in simple for c in value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _build_postgres_dsn(
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
) -> str:
    """Build a libpq connection string with safely quoted credentials."""
    return " ".join(
        [
            f"host={_quote_conninfo_value(host)}",
            f"port={port}",
            f"dbname={_quote_conninfo_value(dbname)}",
            f"user={_quote_conninfo_value(user)}",
            f"password={_quote_conninfo_value(password)}",
        ]
    )


def _persistence_env_overlay() -> dict[str, Any]:
    """Overlay process environment variables onto persistence configuration.

    Returns the persistence field values (process environment overrides
    `.env` and YAML). Keys:
      NOMADICOS_PERSISTENCE_BACKEND -> persistence_backend
      POSTGRES_HOST/PORT/DB/USER/PASSWORD -> postgres_*
      NOMADICOS_TEST_DSN -> dsn (explicit DSN always wins)
    """
    overlay: dict[str, Any] = {}
    backend = os.environ.get("NOMADICOS_PERSISTENCE_BACKEND")
    if backend is not None:
        overlay["persistence_backend"] = backend
    for env_key, field in (
        ("POSTGRES_HOST", "postgres_host"),
        ("POSTGRES_PORT", "postgres_port"),
        ("POSTGRES_DB", "postgres_db"),
        ("POSTGRES_USER", "postgres_user"),
        ("POSTGRES_PASSWORD", "postgres_password"),
    ):
        value = os.environ.get(env_key)
        if value is not None:
            overlay[field] = value
    dsn = os.environ.get("NOMADICOS_TEST_DSN")
    if dsn is not None:
        overlay["dsn"] = dsn
    return overlay


def _inference_env_overlay() -> dict[str, Any]:
    """Overlay inference/model-layout variables onto inference configuration.

    Returns InferenceConfig field values from the PROCESS environment
    (process environment wins over `.env` and YAML; the same keys may be
    set in the root `.env` for convenience). Keys:
      NOMADICOS_LLAMA_SERVER_BIN -> inference.llama_server_bin
      NOMADICOS_LLAMA_SERVER_URL -> llama_server_url
      NOMADICOS_LLAMA_PORT       -> llama_port
      NOMADICOS_MODELS_DIR       -> models_dir
      NOMADICOS_OLLAMA_BASE_URL  -> ollama_base_url
    """
    overlay: dict[str, Any] = {}
    for env_key, field in _INFERENCE_ENV_KEYS.items():
        value = os.environ.get(env_key)
        if value is not None:
            overlay[field] = value
    return overlay


class PersistenceConfig(_Contract):
    persistence_backend: Literal["postgres", "file"] = "file"
    dsn: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "nomadicos"
    postgres_user: str = "nomadicos"
    postgres_password: str | None = None
    task_workspace: str = "data/task-workspaces"
    state_dir: str = "data/state"

    @model_validator(mode="after")
    def _resolve_persistence_dsn(self) -> PersistenceConfig:
        # An explicitly supplied DSN always wins.
        if self.dsn is not None:
            return self
        if self.persistence_backend == "postgres":
            if not self.postgres_password:
                raise ConfigInvalid("persistence_backend='postgres' requires POSTGRES_PASSWORD")
            # Frozen model: mutate via object.__setattr__ so the computed DSN
            # persists on the validated instance (works for both __init__ and
            # model_validate).
            object.__setattr__(
                self,
                "dsn",
                _build_postgres_dsn(
                    self.postgres_host,
                    self.postgres_port,
                    self.postgres_db,
                    self.postgres_user,
                    self.postgres_password,
                ),
            )
        return self


class MemoryConfig(_Contract):
    """Memory brick settings (SPEC §32, §52E-G; Phase 11A). DATA only."""

    state_dir: str = "data/state"
    #: hard cap per memory collection (working/episodic/semantic/procedural
    #: records, object nodes, relations): oldest entries evicted first
    max_records_per_kind: int = Field(default=500, ge=1, le=100_000)
    #: cap for memory context rendered into model prompts (used by 11F/11G)
    context_char_budget: int = Field(default=1200, ge=200, le=8000)
    #: max memories returned by a retrieval query
    max_retrieved_memories: int = Field(default=8, ge=1, le=64)
    #: minimum hybrid similarity score for a memory to be returned
    min_retrieval_score: float = Field(default=0.0, ge=0.0, le=1.0)
    #: max age (days) of a memory before it is considered stale; None = keep
    #: forever. Stale memories are deprioritized but never silently deleted.
    max_memory_retention_days: int | None = Field(default=None, ge=1)
    #: embedding model id for semantic retrieval; empty string disables
    #: semantic search (lexical-only fallback)
    embedding_model: str = "nomic-embed-text"
    #: embedding dimension; must match the model's output dimension
    embedding_dim: int = Field(default=768, ge=1, le=4096)
    #: enable semantic (embedding-based) retrieval; when False, lexical-only
    embedding_enabled: bool = True
    #: weight for lexical score in hybrid retrieval (0.0 = semantic only)
    lexical_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    #: weight for semantic score in hybrid retrieval (0.0 = lexical only)
    semantic_weight: float = Field(default=0.5, ge=0.0, le=1.0)


class EvaluationConfig(_Contract):
    benchmark_version: str = "bench-v1"
    workspace_dir: str = "data/benchmark-workspaces"
    require_fresh_copy: bool = True


class AppConfig(_Contract):
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    models: ModelRoles = Field(default_factory=ModelRoles)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    routing: RouterConfig = Field(default_factory=RouterConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @model_validator(mode="after")
    def _engine_coherence(self) -> AppConfig:
        known = {"llamacpp", "ollama", "mock"}
        if (
            self.inference.default_engine not in known
            or self.inference.fallback_engine not in known
        ):
            raise ValueError("engines must be llamacpp|ollama|mock")
        if self.inference.default_engine == self.inference.fallback_engine:
            raise ValueError("default and fallback inference engines must differ")
        return self


# Root .env (deployment settings) overlays YAML; only loaded for the default
# config so an explicitly provided config file is evaluated on its own.
# Env-var names map to PersistenceConfig fields:
_PERSISTENCE_ENV_KEYS = {
    "NOMADICOS_PERSISTENCE_BACKEND": "persistence_backend",
    "POSTGRES_HOST": "postgres_host",
    "POSTGRES_PORT": "postgres_port",
    "POSTGRES_DB": "postgres_db",
    "POSTGRES_USER": "postgres_user",
    "POSTGRES_PASSWORD": "postgres_password",
    "NOMADICOS_TEST_DSN": "dsn",
}

# Inference/model layout overrides (SPEC §9-11: local-first, portable):
# machine-independent defaults live in InferenceConfig; these variables let a
# host point the runtime at its own llama.cpp build, model directory, or
# Ollama endpoint without editing the committed config file.
_INFERENCE_ENV_KEYS = {
    "NOMADICOS_LLAMA_SERVER_BIN": "llama_server_bin",
    "NOMADICOS_LLAMA_SERVER_URL": "llama_server_url",
    "NOMADICOS_LLAMA_PORT": "llama_port",
    "NOMADICOS_MODELS_DIR": "models_dir",
    "NOMADICOS_OLLAMA_BASE_URL": "ollama_base_url",
}


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML config + root ``.env`` + process environment; fail closed on invalid configuration.

    Loading order (later wins):
      1. field defaults,
      2. root ``.env`` (repository root, never CWD),
      3. YAML config file,
      4. process environment variables.

    Process environment variables override ``.env`` and YAML. The root
    ``.env`` is only loaded for the default (no explicit path) load, so an
    explicitly provided config file is evaluated on its own.
    """
    raw: dict[str, Any] = {}
    candidate = Path(path) if path else None
    if candidate is None and Path("config/config.yaml").exists():
        candidate = Path("config/config.yaml")
    if candidate and candidate.exists():
        try:
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigInvalid(f"cannot parse {candidate}: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigInvalid(f"{candidate} must contain a mapping")
        raw = loaded or {}

    # Root .env (deployment settings) overlays YAML; only loaded for the
    # default config so an explicitly provided config file is evaluated on its own.
    if path is None:
        env_file_vals = _load_env_file(_repo_root() / ".env")
        raw.setdefault("persistence", {})
        for env_key, field in _PERSISTENCE_ENV_KEYS.items():
            if env_key in env_file_vals:
                raw["persistence"][field] = env_file_vals[env_key]
        for env_key, field in _INFERENCE_ENV_KEYS.items():
            if env_key in env_file_vals:
                raw.setdefault("inference", {})
                raw["inference"].setdefault(field, env_file_vals[env_key])

    # Process environment variables override .env and YAML.
    env_overlay = _persistence_env_overlay()
    if env_overlay:
        raw.setdefault("persistence", {})
        raw["persistence"].update(env_overlay)
    inference_overlay = _inference_env_overlay()
    if inference_overlay:
        raw.setdefault("inference", {})
        raw["inference"].update(inference_overlay)

    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigInvalid(f"invalid configuration: {exc}") from exc
