"""Deterministic fake vision + capture pieces for CI (no models, no display)."""

from typing import Any

from nomadicos.models.base import (
    GenerateResult,
    HealthReport,
    ModelCapabilities,
    ModelDescriptor,
    ModelStatus,
    ResourceRequirements,
)
from nomadicos.vision.base import CapturedFrame, ScreenObservation
from nomadicos.vision.parser import parse_observation
from nomadicos.vision.screenshot import FakeScreenshotProvider  # noqa: F401


class FakeGemmaVision:
    """Scripted multimodal fake satisfying LocalModel + VisionModel contracts."""

    def __init__(self, model_id: str = "fake/gemma-vision") -> None:
        self._descriptor = ModelDescriptor(
            model_id=model_id,
            status=ModelStatus.VALIDATED,
            capabilities=ModelCapabilities(
                text_generation=True, vision=True, tool_use=False, max_context_tokens=8192
            ),
            resources=ResourceRequirements(min_ram_mb=512, estimated_load_seconds=0.1),
        )
        self._loaded = False
        self.responses: list[str] = []
        self.describe_calls: list[tuple[bytes, str | None]] = []
        self.fail_next = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    async def load(self) -> None:
        self._loaded = True

    async def unload(self) -> None:
        self._loaded = False

    async def generate(self, request: Any) -> GenerateResult:
        return GenerateResult(text=self.responses.pop(0) if self.responses else "ok")

    async def health(self) -> HealthReport:
        return HealthReport(healthy=True)

    async def describe(
        self, image_bytes: bytes, question: str | None = None, *, max_tokens: int = 512
    ) -> str:
        self.describe_calls.append((image_bytes, question))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated vision failure")
        return (
            self.responses.pop(0)
            if self.responses
            else (
                "A window 'Untitled' with button 'Save' at (120, 340) and text field "
                "'Search' at (40, 20)."
            )
        )

    def queue_response(self, text: str) -> None:
        self.responses.append(text)

    def set_fail_next(self, count: int) -> None:
        self.fail_next = count


def observation_with_description(description: str) -> ScreenObservation:
    observation = ScreenObservation(
        observation_id="obs-test",
        captured_at_ms=0.0,
        content_hash="hashvalue1234",
        width=800,
        height=600,
        description="",
    )
    return parse_observation(observation, description=description)


class RetentionStore:
    """In-memory ScreenStore with BP §159 retention (short-lived captures)."""

    def __init__(self, retention_seconds: float = 60.0) -> None:
        self.retention_seconds = retention_seconds
        self.stored: list[tuple[CapturedFrame, str, float]] = []

    def store(self, frame: CapturedFrame, reason: str) -> str:
        import time

        self.stored.append((frame, reason, time.monotonic()))
        return frame.content_hash

    def evict_expired(self, retention_seconds: float | None = None) -> int:
        import time

        retention = retention_seconds if retention_seconds is not None else self.retention_seconds
        now = time.monotonic()
        keep = [(f, r, t) for f, r, t in self.stored if now - t <= retention]
        removed = len(self.stored) - len(keep)
        self.stored = keep
        return removed


__all__ = [
    "FakeGemmaVision",
    "FakeScreenshotProvider",
    "RetentionStore",
    "observation_with_description",
]
