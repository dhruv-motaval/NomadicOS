"""Configuration selects bricks (SPEC §42), validated at load time.

Loading must fail closed on invalid configuration: a typo in an engine name
must never silently produce a half-wired system.
"""

from __future__ import annotations

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
    profile: str = "FULL_PC_AUTONOMY"
    capabilities: list[CapabilityPattern] = Field(
        default_factory=lambda: [
            "filesystem.*",
            "terminal.*",
            "git.*",
            "process.*",
            "desktop.*",
        ]
    )
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


class RouterConfig(_Contract):
    mode: RoutingMode = "BALANCED"
    minimum_quality: dict[str, float] = Field(
        default_factory=lambda: {"FAST": 0.55, "BALANCED": 0.7, "QUALITY": 0.85}
    )
    historical_weight: float = Field(default=0.3, ge=0.0, le=1.0)


class OrchestrationConfig(_Contract):
    engine: Literal["langgraph"] = "langgraph"


class PersistenceConfig(_Contract):
    dsn: str | None = None
    task_workspace: str = "data/task-workspaces"
    state_dir: str = "data/state"


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


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load YAML config; a missing file means defaults (SPEC §42)."""
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
    try:
        return AppConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigInvalid(f"invalid configuration: {exc}") from exc
