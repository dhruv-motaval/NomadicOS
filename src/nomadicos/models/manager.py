"""Model Manager (BP §62, §96; ADR-0001/0006): registry + sequential residency.

The manager owns load/unload/health across the enabled model set with
**sequential residency as the default** (ADR-0006): at most one model holds
VRAM/RAM unless resources allow more. Model selection consults loading cost.
"""

import asyncio
import time
from typing import Any

from nomadicos.core.errors import ModelResourceError, ModelUnavailable
from nomadicos.core.logging import get_logger
from nomadicos.models.base import (
    GenerateRequest,
    GenerateResult,
    HealthReport,
    LocalModel,
    ModelDescriptor,
    ModelStatus,
)

logger = get_logger("models.manager")


class ModelManager:
    def __init__(self, *, max_resident: int = 1) -> None:
        self._models: dict[str, LocalModel] = {}
        self._status: dict[str, ModelStatus] = {}
        self._resident: list[str] = []
        self._max_resident = max(1, max_resident)
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- registration

    def register(self, model: LocalModel, *, status: ModelStatus = ModelStatus.VALIDATED) -> None:
        model_id = model.descriptor.model_id
        if model_id in self._models:
            raise ValueError(f"model already registered: {model_id}")
        self._models[model_id] = model
        self._status[model_id] = status

    def set_status(self, model_id: str, status: ModelStatus) -> None:
        if model_id not in self._status:
            raise ModelUnavailable(f"unknown model: {model_id}")
        self._status[model_id] = status

    async def unload_if_resident(self, model_id: str) -> None:
        model = self._models.get(model_id)
        if model is not None and model.is_loaded:
            await model.unload()
            if model_id in self._resident:
                self._resident.remove(model_id)

    def list_available(self, *, enabled_only: bool = True) -> list[ModelDescriptor]:
        eligible = (ModelStatus.ENABLED, ModelStatus.BENCHMARKED)
        return [
            m.descriptor
            for mid, m in self._models.items()
            if not enabled_only or self._status[mid] in eligible
        ]

    def get(self, model_id: str) -> LocalModel:
        if model_id not in self._models:
            raise ModelUnavailable(f"unknown model: {model_id}")
        blocked = (ModelStatus.DISABLED, ModelStatus.QUARANTINED, ModelStatus.DEPRECATED)
        if self._status[model_id] in blocked:
            raise ModelUnavailable(f"model is not enabled: {model_id}")
        return self._models[model_id]

    # ------------------------------------------------------------- residency

    @property
    def resident(self) -> list[str]:
        return list(self._resident)

    async def ensure_loaded(self, model_id: str) -> LocalModel:
        """Load a model, swapping out others when residency is full (ADR-0006)."""
        async with self._lock:
            model = self.get(model_id)
            if model.is_loaded:
                return model
            while len(self._resident) >= self._max_resident:
                victim_id = self._resident.pop(0)
                victim = self._models[victim_id]
                await victim.unload()
                logger.info("model unloaded model=%s", victim_id)
            started = time.monotonic()
            await model.load()
            self._resident.append(model_id)
            logger.info(
                "model loaded model=%s load_latency_ms=%.1f",
                model_id,
                (time.monotonic() - started) * 1000,
            )
            return model

    async def unload(self, model_id: str) -> None:
        async with self._lock:
            model = self._models.get(model_id)
            if model is None:
                raise ModelUnavailable(f"unknown model: {model_id}")
            await model.unload()
            if model_id in self._resident:
                self._resident.remove(model_id)

    def load_cost(self, model_id: str) -> float:
        """Estimated load cost used by ModelSelector (BP §8.5, ADR-0006)."""
        model = self._models[model_id]
        if model.is_loaded:
            return 0.0
        return model.resource_requirements.estimated_load_seconds

    # ---------------------------------------------------------------- health

    async def health(self, model_id: str) -> HealthReport:
        model = self.get(model_id)
        return await model.health()

    async def generate(self, model_id: str, request: GenerateRequest) -> GenerateResult:
        model = self.get(model_id)
        if not model.is_loaded:
            raise ModelResourceError(
                f"model not resident: {model_id}",
                context={"hint": "call ensure_loaded first"},
            )
        return await model.generate(request)

    def snapshot(self) -> dict[str, Any]:
        return {
            "registered": list(self._models),
            "resident": list(self._resident),
            "status": {mid: s.value for mid, s in self._status.items()},
        }


__all__ = ["ModelManager"]
