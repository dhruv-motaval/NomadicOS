"""Local model runtime interfaces (BP §8, §62, §209)."""

from nomadicos.models.base import (
    GenerateRequest,
    GenerateResult,
    HealthReport,
    LocalModel,
    ModelCapabilities,
    ModelDescriptor,
    ModelStatus,
    ResourceRequirements,
)
from nomadicos.models.fake import FakeLocalModel, fake_vision_model
from nomadicos.models.manager import ModelManager
from nomadicos.models.ollama_adapter import OllamaModel

__all__ = [
    "FakeLocalModel",
    "GenerateRequest",
    "GenerateResult",
    "HealthReport",
    "LocalModel",
    "ModelCapabilities",
    "ModelDescriptor",
    "ModelManager",
    "ModelStatus",
    "OllamaModel",
    "ResourceRequirements",
    "fake_vision_model",
]
