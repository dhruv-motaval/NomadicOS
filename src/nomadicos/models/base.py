"""LocalModel interface (BP §8.1, §62, §96, §209) — runtime replaceable (ADR-0001).

Interfaces only in this module: llama-cpp-python or any other inference backend
plugs in behind `LocalModel` and never leaks into the Agent Runtime.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

BoundedStr = Annotated[str, StringConstraints(min_length=1, max_length=1_000_000)]


class ModelStatus(StrEnum):
    """Model registry lifecycle states (BP §276)."""

    DISCOVERED = "discovered"
    VALIDATED = "validated"
    BENCHMARKED = "benchmarked"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"
    DEPRECATED = "deprecated"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCapabilities(StrictModel):
    text_generation: bool = True
    vision: bool = False  # VisionModel capability (ADR-0004)
    tool_use: bool = False
    long_context: bool = False
    max_context_tokens: int | None = Field(default=None, ge=0)


class ResourceRequirements(StrictModel):
    """Hardware requirements consumed by the ModelSelector (BP §53, §170)."""

    model_config = ConfigDict(extra="forbid")

    min_ram_mb: int = Field(default=0, ge=0)
    min_vram_mb: int = Field(default=0, ge=0)
    gpu_required: bool = False
    estimated_load_seconds: float = Field(default=0.0, ge=0)


class ModelDescriptor(StrictModel):
    """Registry record for one local model artifact (BP §148, §150-151, §276)."""

    model_config = ConfigDict(extra="forbid")

    model_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    display_name: str | None = Field(default=None, max_length=256)
    path: str | None = None
    format: str = "gguf"
    status: ModelStatus = ModelStatus.DISCOVERED
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    checksum_sha256: str | None = None
    source: str | None = None  # provenance (BP §150)
    version: str | None = None


class GenerateRequest(StrictModel):
    prompt: BoundedStr
    system: str | None = Field(default=None, max_length=100_000)
    max_output_tokens: int = Field(default=4096, ge=1, le=128_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    stop: list[str] = Field(default_factory=list)


class GenerateResult(StrictModel):
    text: str = ""
    finish_reason: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)


class HealthReport(StrictModel):
    healthy: bool
    detail: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)


class LocalModel(ABC):
    """The only model abstraction the Agent Runtime may know (BP §209).

    Implementations: llama-cpp-python adapter first (ADR-0001), fake adapter for
    tests, later runtimes behind the same interface.
    """

    @property
    @abstractmethod
    def descriptor(self) -> ModelDescriptor: ...

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.descriptor.capabilities

    @property
    def resource_requirements(self) -> ResourceRequirements:
        return self.descriptor.resources

    @abstractmethod
    async def load(self) -> None: ...

    @abstractmethod
    async def unload(self) -> None: ...

    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResult: ...

    @abstractmethod
    async def health(self) -> HealthReport: ...


class VisionModel(ABC):
    """Vision capability interface (ADR-0004, BP §209).

    A multimodal LocalModel may implement this; the Agent Runtime asks for the
    capability, never for a concrete model.
    """

    @abstractmethod
    async def describe(
        self,
        image_bytes: bytes,
        question: str | None = None,
        *,
        max_tokens: int = 512,
    ) -> str: ...


__all__ = [
    "GenerateRequest",
    "GenerateResult",
    "HealthReport",
    "LocalModel",
    "ModelCapabilities",
    "ModelDescriptor",
    "ModelStatus",
    "ResourceRequirements",
    "VisionModel",
]
