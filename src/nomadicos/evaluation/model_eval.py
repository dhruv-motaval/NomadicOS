"""Model performance tracking (BP §64, §148): real-world model benchmarking.

Synthetic benchmarks are insufficient (BP §64) — actual task outcomes feed the
ModelSelector statistics across sessions (BP §386-387).
"""

import time
from typing import Any

from nomadicos.core.logging import get_logger

logger = get_logger("evaluation.model_eval")


class ModelPerformanceTracker:
    """In-memory aggregation; the PostgreSQL mirror lands via ModelRepository
    (Phase 1) when persistence is wired into the Agent Runtime."""

    def __init__(self) -> None:
        self._stats: dict[tuple[str, str], dict[str, Any]] = {}

    def record(
        self,
        *,
        model_id: str,
        task_family: str,
        success: bool,
        duration_seconds: float,
        verified: bool,
    ) -> None:
        key = (model_id, task_family)
        entry = self._stats.setdefault(
            key,
            {
                "attempts": 0,
                "successes": 0,
                "verified_successes": 0,
                "total_duration": 0.0,
                "last_used": None,
            },
        )
        entry["attempts"] += 1
        if success:
            entry["successes"] += 1
        if success and verified:
            entry["verified_successes"] += 1
        entry["total_duration"] += duration_seconds
        entry["last_used"] = time.time()

    def quality_score(self, model_id: str, task_family: str) -> float:
        """0-10: verified success rate × attempt confidence (BP §148)."""
        entry = self._stats.get((model_id, task_family))
        if entry is None or entry["attempts"] == 0:
            return 0.0
        success_rate = entry["successes"] / entry["attempts"]
        verified_rate = entry["verified_successes"] / entry["attempts"]
        attempts_confidence = min(1.0, entry["attempts"] / 10.0)
        return round((success_rate * 0.6 + verified_rate * 0.4) * 10.0 * attempts_confidence, 3)

    def median_latency(self, model_id: str, task_family: str) -> float | None:
        entry = self._stats.get((model_id, task_family))
        if entry is None or entry["attempts"] == 0:
            return None
        return round(entry["total_duration"] / entry["attempts"], 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            f"{mid}::{family}": {**entry, "quality_score": self.quality_score(mid, family)}
            for (mid, family), entry in self._stats.items()
        }


__all__ = ["ModelPerformanceTracker"]
