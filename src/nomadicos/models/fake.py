"""Fake model adapter — full-featured test double (BP §211: synthetic fixtures).

Lets every downstream subsystem (Agent Runtime, selectors, evaluation) be
developed and CI-tested without models, GPU, or network (analysis §2.2.4).
"""

import time

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


class FakeLocalModel(LocalModel):
    def __init__(
        self,
        model_id: str = "fake/model",
        *,
        responses: list[str] | None = None,
        fail_generate: bool = False,
        load_latency_ms: float = 0.0,
        capabilities: ModelCapabilities | None = None,
        resources: ResourceRequirements | None = None,
        vision: bool = False,
    ) -> None:
        self._descriptor = ModelDescriptor(
            model_id=model_id,
            display_name=f"Fake {model_id}",
            status=ModelStatus.VALIDATED,
            capabilities=capabilities
            or ModelCapabilities(vision=vision, tool_use=True, long_context=False),
            resources=resources or ResourceRequirements(min_ram_mb=64),
        )
        self._responses = list(responses or ["ok"])
        self._fail_generate = fail_generate
        self._load_latency_ms = load_latency_ms
        self._loaded = False
        self.generate_calls: list[GenerateRequest] = []
        self.load_calls = 0
        self.unload_calls = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self.load_calls += 1
        if self._load_latency_ms:
            time.sleep(self._load_latency_ms / 1000)
        self._loaded = True

    async def unload(self) -> None:
        self.unload_calls += 1
        self._loaded = False

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        if not self._loaded:
            raise RuntimeError(f"model {self._descriptor.model_id} is not loaded")
        self.generate_calls.append(request)
        if self._fail_generate:
            raise RuntimeError("fake model failure")
        text = self._responses.pop(0) if self._responses else "ok"
        return GenerateResult(
            text=text,
            finish_reason="stop",
            input_tokens=len(request.prompt) // 4,
            output_tokens=len(text) // 4,
            latency_ms=1.0,
        )

    async def health(self) -> HealthReport:
        return HealthReport(healthy=self._loaded or True, latency_ms=0.5)

    # ---- test hooks ----
    def queue_response(self, text: str) -> None:
        self._responses.append(text)

    def set_fail_generate(self, fail: bool) -> None:
        self._fail_generate = fail


def fake_vision_model(model_id: str = "fake/vision") -> LocalModel:
    """A fake multimodal model that also satisfies VisionModel (ADR-0004)."""

    class _VisionCapable(FakeLocalModel):
        async def describe(
            self, image_bytes: bytes, question: str | None = None, *, max_tokens: int = 512
        ) -> str:
            return f"vision-description:{len(image_bytes)}b"

    return _VisionCapable(
        model_id,
        capabilities=ModelCapabilities(vision=True, tool_use=True),
    )


__all__ = ["FakeLocalModel", "fake_vision_model"]
