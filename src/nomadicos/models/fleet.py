"""FleetSynchronizer (BP §8.4, §148, §187): the model fleet self-updates.

The owner can add, remove, or re-pull models on the Ollama server at any time;
the next sync makes NomadicOS match reality without a restart:

- NEW model appears      → registered, capability-verified, health-checked → enabled
- model DISAPPEARS       → quarantined (never selected; history retained)
- model CHANGED weights  → quarantined pending re-validation (BP §107/§151)
- still healthy          → stays enabled

Selection then adapts automatically because the ModelSelector only sees
enabled models (BP §97/§320), and per-family history survives in the
performance tracker (BP §386-387).
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from nomadicos.core.errors import NomadicError
from nomadicos.core.logging import get_logger
from nomadicos.models.base import ModelStatus
from nomadicos.models.manager import ModelManager
from nomadicos.models.ollama_adapter import OllamaModel

logger = get_logger("models.fleet")

_SYNC_COOLDOWN_SECONDS = 30.0


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    healthy: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def quiet(self) -> bool:
        return not (self.added or self.removed or self.changed)


class FleetSynchronizer:
    """Keeps the ModelManager aligned with the live Ollama server."""

    def __init__(
        self,
        manager: ModelManager,
        *,
        performance: Any | None = None,
        sync_cooldown_seconds: float = _SYNC_COOLDOWN_SECONDS,
        discover_fn: Any | None = None,
    ) -> None:
        self._manager = manager
        self._performance = performance
        self._discover_fn = discover_fn
        self._last_sync: float = 0.0
        self._cooldown = sync_cooldown_seconds
        self._known_digests: dict[str, str | None] = {}

    @property
    def known_digests(self) -> dict[str, str | None]:
        return dict(self._known_digests)

    async def sync(self, *, force: bool = False) -> SyncReport:
        """Discover the live fleet and reconcile the registry (BP §148)."""
        report = SyncReport()
        started = time.monotonic()
        if not force and (time.monotonic() - self._last_sync) < self._cooldown:
            return report  # cooldown: protect the server (BP §242)

        try:
            if self._discover_fn is not None:
                result = self._discover_fn()
                if asyncio.iscoroutine(result):
                    result = await result
                live = result
            else:
                live = await OllamaModel.discover()
        except NomadicError as exc:
            logger.warning("fleet sync skipped: %s", exc)
            return report
        live_ids = {m.descriptor.model_id: m for m in live}

        # 1. NEW models → register → health-check → enable.
        for model_id, model in live_ids.items():
            if self._is_registered(model_id):
                continue
            try:
                self._manager.register(model, status=ModelStatus.ENABLED)
            except ValueError:
                pass  # registered by a concurrent sync
            health = await model.health()
            if health.healthy:
                self._known_digests[model_id] = model.descriptor.checksum_sha256
                report.healthy.append(model_id)
                logger.info("fleet: model added and healthy model=%s", model_id)
            else:
                self._manager.set_status(model_id, ModelStatus.QUARANTINED)
                report.failed.append(model_id)
                logger.warning(
                    "fleet: model added but unhealthy (quarantined) model=%s", model_id
                )
            report.added.append(model_id)

        # 2. Disappeared models → quarantine (history retained, never deleted).
        for model_id in self._manager.snapshot()["status"]:
            if not model_id.startswith("ollama/"):
                continue
            if model_id in live_ids:
                continue
            if self._manager.snapshot()["status"].get(model_id) != ModelStatus.QUARANTINED.value:
                self._manager.set_status(model_id, ModelStatus.QUARANTINED)
                report.removed.append(model_id)
                logger.info("fleet: model removed from server (quarantined) model=%s", model_id)

        # 3. Changed weights (same id, new digest) → quarantine for re-validation.
        for model_id, model in live_ids.items():
            if not self._is_registered(model_id):
                continue
            new_digest = model.descriptor.checksum_sha256
            old_digest = self._known_digests.get(model_id)
            if old_digest is not None and new_digest is not None and old_digest != new_digest:
                self._manager.set_status(model_id, ModelStatus.QUARANTINED)
                report.changed.append(model_id)
                logger.warning(
                    "fleet: model weights changed (quarantined) model=%s old=%s new=%s",
                    model_id,
                    (old_digest or "")[:12],
                    (new_digest or "")[:12],
                )
                continue
            # Still-current models: health re-probe (BP §62).
            health = await model.health()
            if health.healthy:
                report.healthy.append(model_id)
                self._known_digests[model_id] = new_digest
            else:
                self._manager.set_status(model_id, ModelStatus.DISABLED)
                report.failed.append(model_id)

        self._last_sync = time.monotonic()
        report.duration_ms = (time.monotonic() - started) * 1000
        if not report.quiet:
            logger.info(
                "fleet synced added=%d removed=%d changed=%d healthy=%d failed=%d",
                len(report.added),
                len(report.removed),
                len(report.changed),
                len(report.healthy),
                len(report.failed),
            )
        return report

    def _is_registered(self, model_id: str) -> bool:
        return model_id in self._manager.snapshot()["status"]


__all__ = ["FleetSynchronizer", "SyncReport"]
