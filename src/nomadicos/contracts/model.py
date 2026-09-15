"""Model registry + routing contracts (SPEC §12-13, §43, §52A-C)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.kernel.config import EngineName, RoutingMode

ModelRole = Literal["tiny", "worker", "coding", "reasoning", "critic", "evaluator"]
ModelSource = Literal["models_dir", "llamacpp", "ollama", "ollama_store", "mock", "manual"]


class ModelHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class CapabilityTag(StrEnum):
    """What a model can be asked to do, from measurements — not claims."""

    TEXT = "text"
    TOOL_USE = "tool_use"
    CODING = "coding"
    TESTING = "testing"
    REASONING = "reasoning"


class TaskType(StrEnum):
    CLASSIFICATION = "classification"
    CHAT = "chat"
    SIMPLE_FILE = "simple_file"
    TERMINAL = "terminal"
    CODING = "coding"
    TESTING = "testing"
    REASONING = "reasoning"
    PLANNING = "planning"


class ModelRecord(Contract):
    """Registry entry; metrics stay null until actually measured (§12)."""

    model_id: str
    engine: EngineName
    roles: list[ModelRole] = Field(default_factory=list)
    capabilities: list[CapabilityTag] = Field(default_factory=list)
    context_window: int = Field(default=8192, gt=0)
    health: ModelHealth = ModelHealth.UNKNOWN
    enabled: bool = True
    source: ModelSource = "manual"
    #: path/file name for models_dir-sourced entries; engine name otherwise.
    location: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_roles(self) -> ModelRecord:
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("duplicate roles in model record")
        return self


class MetricsBundle(Contract):
    """Aggregated measurements. `samples == 0` means NOTHING is known."""

    samples: int = 0
    success_rate: float | None = None
    goal_success_rate: float | None = None
    tool_success_rate: float | None = None
    repair_success_rate: float | None = None
    critic_quality: float | None = None
    median_latency_s: float | None = None
    p95_latency_s: float | None = None
    avg_steps: float | None = None
    avg_retries: float | None = None

    @model_validator(mode="after")
    def _coherent(self) -> MetricsBundle:
        if self.samples == 0 and any(
            v is not None
            for v in (
                self.success_rate,
                self.goal_success_rate,
                self.tool_success_rate,
                self.critic_quality,
            )
        ):
            raise ValueError("metrics without samples are invented numbers (SPEC §12)")
        return self


class ModelMetrics(Contract):
    """Benchmark vs production evidence kept strictly separate (§52C)."""

    model_id: str
    #: task class -> measurements collected by the benchmark runner
    benchmark: dict[str, MetricsBundle] = Field(default_factory=dict)
    #: task class -> measurements from validated real production outcomes
    production: dict[str, MetricsBundle] = Field(default_factory=dict)


class TaskRequirements(Contract):
    """Output of the task analyzer; fully deterministic (§13)."""

    task_type: TaskType
    capabilities: list[CapabilityTag] = Field(default_factory=list)
    difficulty: float = Field(ge=0.0, le=1.0)
    needs_large_context: bool = False
    latency: RoutingMode = "BALANCED"
    verification_required: bool = True

    @model_validator(mode="after")
    def _coding_needs_tools(self) -> TaskRequirements:
        if (
            self.task_type
            in (TaskType.CODING, TaskType.TESTING, TaskType.SIMPLE_FILE, TaskType.TERMINAL)
            and CapabilityTag.TOOL_USE not in self.capabilities
        ):
            raise ValueError("execution-oriented tasks must require tool_use")
        return self
