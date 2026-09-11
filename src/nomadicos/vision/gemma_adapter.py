"""Gemma-family local vision adapter (ADR-0004, BP §1.4, §10).

Implements both LocalModel and VisionModel: a multimodal LocalModel satisfies
the VisionModel capability. Transformers is an optional hardware-dependent
dependency, imported lazily — CI uses the fake vision model (ADR-0024).
"""

import base64
import io
import time
from typing import Any

from nomadicos.core.errors import ModelResourceError
from nomadicos.core.logging import get_logger
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

logger = get_logger("vision.gemma")

_TransformerModel = Any
_TransformerProcessor = Any

try:  # optional hardware dependency — names bound on every path below
    from transformers import AutoModelForCausalLM, AutoProcessor

    TRANSFORMERS_AVAILABLE = True
except ImportError:  # pragma: no cover — CI has no transformers
    AutoModelForCausalLM = None
    AutoProcessor = None
    TRANSFORMERS_AVAILABLE = False


class GemmaVisionModel(LocalModel):
    """Local Gemma-class multimodal model (satisfies VisionModel, ADR-0004).

    All transformers specifics stay in this adapter — never in the Agent
    Runtime or Vision Engine.
    """

    def __init__(
        self,
        model_id: str,
        model_path: str,
        *,
        max_context_tokens: int = 8192,
    ) -> None:
        if not TRANSFORMERS_AVAILABLE:
            raise ModelResourceError(
                "transformers is not installed",
                context={"install": "pip install transformers torch pillow"},
            )
        self._descriptor = ModelDescriptor(
            model_id=model_id,
            path=model_path,
            status=ModelStatus.VALIDATED,
            capabilities=ModelCapabilities(
                text_generation=True,
                vision=True,  # VisionModel capability (ADR-0004)
                tool_use=False,
                long_context=max_context_tokens >= 32768,
                max_context_tokens=max_context_tokens,
            ),
            resources=ResourceRequirements(min_ram_mb=8192, estimated_load_seconds=20.0),
        )
        self._model_path = model_path
        self._model: _TransformerModel | None = None
        self._processor: _TransformerProcessor | None = None

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        if self._model is not None:
            return
        import asyncio

        started = time.monotonic()

        def _load() -> None:
            self._processor = AutoProcessor.from_pretrained(self._model_path)
            self._model = AutoModelForCausalLM.from_pretrained(self._model_path)

        await asyncio.get_running_loop().run_in_executor(None, _load)
        logger.info(
            "gemma vision model loaded model=%s latency_ms=%.1f",
            self._descriptor.model_id,
            (time.monotonic() - started) * 1000,
        )

    async def unload(self) -> None:
        self._model = None
        self._processor = None
        import gc

        gc.collect()
        logger.info("gemma vision model unloaded model=%s", self._descriptor.model_id)

    # ---------------------------------------------------------------- VisionModel

    async def describe(
        self,
        image_bytes: bytes,
        question: str | None = None,
        *,
        max_tokens: int = 512,
    ) -> str:
        """Describe a screenshot locally (BP §51). Never sends bytes off-machine."""
        if self._model is None:
            raise ModelResourceError("vision model not loaded")
        import asyncio

        prompt = (
            question
            or "Describe this screen: visible windows, buttons, text fields, and their "
            "approximate positions."
        )

        def _run() -> str:
            from PIL import Image

            image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            assert self._processor is not None and self._model is not None
            inputs = self._processor(text=prompt, images=image, return_tensors="pt")
            output = self._model.generate(**inputs, max_new_tokens=max_tokens)
            decoded = self._processor.batch_decode(output, skip_special_tokens=True)
            return decoded[0].strip() if decoded else ""

        return await asyncio.get_running_loop().run_in_executor(None, _run)

    # ---------------------------------------------------------------- LocalModel

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        if self._model is None:
            raise ModelResourceError("vision model not loaded")
        started = time.monotonic()
        text = await self.describe(
            base64.b64decode(b""), request.prompt, max_tokens=request.max_output_tokens
        )
        return GenerateResult(
            text=text, finish_reason="stop", latency_ms=(time.monotonic() - started) * 1000
        )

    async def health(self) -> HealthReport:
        return HealthReport(
            healthy=self._model is not None or TRANSFORMERS_AVAILABLE,
            detail=None if self._model is not None else "vision model not loaded",
        )


class InProcessVisionModel:
    """Capability-only wrapper for fake/CI use — satisfies VisionModel."""

    def __init__(self, responder=None) -> None:
        self._responder = responder
        self.describe_calls: list[tuple[bytes, str | None]] = []
        self.fail_next = 0

    async def describe(
        self, image_bytes: bytes, question: str | None = None, *, max_tokens: int = 512
    ) -> str:
        self.describe_calls.append((image_bytes, question))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("vision failure")
        if self._responder is not None:
            return self._responder(image_bytes, question)
        return "A code editor window is open with a test explorer panel."

    async def health(self) -> HealthReport:
        return HealthReport(healthy=True)


__all__ = ["GemmaVisionModel", "InProcessVisionModel", "TRANSFORMERS_AVAILABLE"]
