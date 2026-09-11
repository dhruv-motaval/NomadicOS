"""llama-cpp-python adapter (ADR-0001) — the only llama.cpp-aware module.

Imported lazily: llama-cpp-python is an optional hardware-dependent dependency.
In shared CI the fake adapter (models.fake.FakeLocalModel) is used instead
(ADR-0024).
"""

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

logger = get_logger("models.llamacpp")

try:  # optional dependency
    from llama_cpp import Llama  # type: ignore[import-not-found]

    LLAMA_CPP_AVAILABLE = True
except ImportError:  # pragma: no cover — CI has no llama-cpp-python
    Llama = None
    LLAMA_CPP_AVAILABLE = False


class LlamaCppModel(LocalModel):
    """In-process GGUF model via llama-cpp-python (ADR-0001).

    All llama.cpp specifics stay inside this adapter; the Agent Runtime only
    ever sees `LocalModel`.
    """

    def __init__(
        self,
        model_id: str,
        path: str,
        *,
        n_ctx: int = 8192,
        n_gpu_layers: int = -1,  # -1 = offload all (when GPU available)
        n_threads: int | None = None,
        vision: bool = False,
    ) -> None:
        if not LLAMA_CPP_AVAILABLE:
            raise ModelResourceError(
                "llama-cpp-python is not installed",
                context={"install": "pip install llama-cpp-python"},
            )
        self._descriptor = ModelDescriptor(
            model_id=model_id,
            path=path,
            status=ModelStatus.VALIDATED,
            capabilities=ModelCapabilities(
                text_generation=True,
                vision=vision,
                long_context=n_ctx >= 32768,
                max_context_tokens=n_ctx,
            ),
            resources=ResourceRequirements(estimated_load_seconds=2.0),
        )
        self._path = path
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._n_threads = n_threads
        self._llm: Any = None

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    async def load(self) -> None:
        if self._llm is not None:
            return
        started = time.monotonic()
        loop = __import__("asyncio").get_running_loop()
        assert Llama is not None
        self._llm = await loop.run_in_executor(
            None,
            lambda: Llama(
                model_path=self._path,
                n_ctx=self._n_ctx,
                n_gpu_layers=self._n_gpu_layers,
                n_threads=self._n_threads or 0,
                verbose=False,
            ),
        )
        logger.info(
            "llama.cpp model loaded model=%s latency_ms=%.1f",
            self._descriptor.model_id,
            (time.monotonic() - started) * 1000,
        )

    async def unload(self) -> None:
        self._llm = None
        import gc

        gc.collect()
        logger.info("llama.cpp model unloaded model=%s", self._descriptor.model_id)

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        if self._llm is None:
            raise ModelResourceError("model not loaded")
        import asyncio

        started = time.monotonic()
        loop = asyncio.get_running_loop()

        def _run() -> Any:
            messages: list[dict[str, str]] = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.append({"role": "user", "content": request.prompt})
            return self._llm.create_chat_completion(
                messages=messages,
                max_tokens=request.max_output_tokens,
                temperature=request.temperature,
                stop=request.stop or None,
            )

        raw = await loop.run_in_executor(None, _run)
        latency_ms = (time.monotonic() - started) * 1000
        choice = (raw.get("choices") or [{}])[0]
        usage = raw.get("usage") or {}
        return GenerateResult(
            text=(choice.get("message") or {}).get("content", ""),
            finish_reason=choice.get("finish_reason"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
        )

    async def health(self) -> HealthReport:
        return HealthReport(
            healthy=self._llm is not None or LLAMA_CPP_AVAILABLE,
            detail=None if self._llm is not None else "model not loaded",
        )


__all__ = ["LLAMA_CPP_AVAILABLE", "LlamaCppModel"]
