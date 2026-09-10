"""ModelSelector (BP §7, §8.4-8.6, §97, §149, §188, §225-226, §320).

Flow (BP §97 + answers Section H):
task analysis → required capabilities → registry → capability filter →
benchmark history → real experience → hardware → resource requirements →
policy → task criticality → selection.

Returns: primary_model + fallback_local_models + score + reason. No cloud
fallback ever (BP §149, §188). Selection is explainable (BP §225-226) and
learns across sessions from model performance statistics (BP §386-387).
"""

from dataclasses import dataclass, field
from typing import Any

from nomadicos.agent.selector_policies import SelectionPolicy
from nomadicos.core.errors import ModelUnavailable
from nomadicos.core.logging import get_logger
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.models.base import ModelCapabilities
from nomadicos.models.manager import ModelManager

logger = get_logger("agent.selector")


@dataclass(frozen=True, slots=True)
class SelectionCandidate:
    model_id: str
    score: float
    reason: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HardwareConstraints:
    """From the HardwareProfile (BP §170, ADR-0002)."""

    ram_mb: int = 8192
    vram_mb: int = 0
    gpu_available: bool = False


class ModelSelector:
    def __init__(
        self,
        manager: ModelManager,
        tracker: ModelPerformanceTracker,
        policy: SelectionPolicy | None = None,
        hardware: HardwareConstraints | None = None,
    ) -> None:
        self._manager = manager
        self._tracker = tracker
        self._policy = policy or SelectionPolicy()
        self._hardware = hardware or HardwareConstraints()

    def select(
        self,
        *,
        task_family: str,
        required_capabilities: set[str] | None = None,
        project_context: str | None = None,
    ) -> tuple[str, list[str], float, dict[str, Any]]:
        """BP §97: returns (primary, fallbacks, score, reason). Fail closed when
        no candidate qualifies (BP §149: BLOCK/ASK USER, never cloud)."""
        required = {c.lower() for c in (required_capabilities or set())}
        candidates: list[SelectionCandidate] = []

        for descriptor in self._manager.list_available():
            caps = descriptor.capabilities
            if not self._capabilities_satisfied(caps, required):
                continue
            if not self._hardware_fits(descriptor.model_id):
                continue
            score, reason = self._score(descriptor, task_family, project_context)
            candidates.append(SelectionCandidate(descriptor.model_id, score, reason))

        if not candidates:
            raise ModelUnavailable(
                "no local model satisfies the required capabilities for this task "
                f"family {task_family!r} under current constraints — BLOCK "
                "(BP §149, §188: no cloud fallback)",
                context={"task_family": task_family, "required": sorted(required)},
            )

        candidates.sort(key=lambda c: (-c.score, c.model_id))
        primary = candidates[0]
        fallbacks = [c.model_id for c in candidates[1 : 1 + self._policy.max_fallbacks]]
        logger.info(
            "model selected primary=%s score=%s fallbacks=%s task_family=%s",
            primary.model_id,
            primary.score,
            fallbacks,
            task_family,
        )
        return primary.model_id, fallbacks, primary.score, primary.reason

    # ---------------------------------------------------------------- scoring

    def _score(
        self, descriptor: Any, task_family: str, project_context: str | None
    ) -> tuple[float, dict[str, Any]]:
        """BP §320: capability score + historical success; project-specific
        experience wins over global score (BP §148, §320)."""
        model_id = descriptor.model_id
        history = self._history_score(model_id, task_family, project_context)
        capability = self._capability_score(model_id, task_family, descriptor)
        # BP §320: capability + history composite; project bonus may apply.
        score = (
            self._policy.capability_weight * capability
            + self._policy.history_weight * history
        )
        reason: dict[str, Any] = {
            "capability_score": round(capability, 3),
            "history_score": round(history, 3),
            "task_family": task_family,
        }
        if project_context:
            project_stats = self._tracker._stats.get(  # noqa: SLF001 — internal read
                (model_id, f"{task_family}:{project_context}")
            )
            learned = self._policy.min_attempts_for_learning
            if project_stats and project_stats["attempts"] >= learned:
                score += self._policy.project_specificity_bonus
                reason["project_specific"] = True
        return round(score, 3), reason

    def _history_score(self, model_id: str, task_family: str, project_context: str | None) -> float:
        """Verified success rate from real experience (BP §64, §387). Unproven
        models stay capped — evidence, not promises (BP §67, §366)."""
        global_score = self._tracker.quality_score(model_id, task_family)
        attempts = self._tracker._stats.get((model_id, task_family), {}).get("attempts", 0)  # noqa: SLF001
        if attempts < self._policy.min_attempts_for_learning:
            return min(global_score, self._policy.unverified_quality_cap)
        if project_context:
            project_score = self._tracker.quality_score(
                model_id, f"{task_family}:{project_context}"
            )
            if project_score > 0:
                return max(global_score, project_score)
        return global_score

    def _capability_score(
        self, model_id: str, task_family: str, descriptor: Any = None
    ) -> float:
        """BP §320: family fit + model capability. Until Phase 13 benchmarks
        arrive, intelligence is estimated from model size (params ≈ capability)
        plus verified capability flags — NOT a static constant, so bigger
        reasoners actually win planning/reasoning families while small models
        stay attractive only where speed matters more than depth."""
        family_fit = {
            "coding": 7.0,
            "reasoning": 7.0,
            "research": 6.5,
            "vision": 6.0,
            "automation": 6.0,
            "general": 6.5,
        }
        score = family_fit.get(task_family, 6.0)

        caps = getattr(descriptor, "capabilities", None) if descriptor else None
        # Size-based intelligence estimate (BP §148 metadata until benchmarks).
        # Derive params from model id (e.g. "14b", "30b-a3b") — discovered
        # Ollama models carry the size in their name.
        import re as _re

        size_match = _re.search(r"(\d+(?:\.\d+)?)b\b", model_id.lower())
        params_b = float(size_match.group(1)) if size_match else 7.0
        # Diminishing returns: 4b→~6.6, 8b→~7.2, 14b→~7.9, 30b→~8.5
        size_score = min(6.0 + (params_b ** 0.5) * 0.55, 9.5)

        if task_family == "general":
            # Speed-first: chat and greetings want the small fast model;
            # deep reasoning is routed to its own family.
            return round(6.5 - 0.06 * params_b, 3)

        score = (score + size_score) / 2
        if caps is not None:
            if getattr(caps, "tool_use", False):
                score += 0.4  # structured output matters for every family
            if task_family == "vision" and getattr(caps, "vision", False):
                score += 1.5  # hard requirement practically
        # Thinking/reasoning channel (qwen3 hybrid, gpt-oss levels): only these
        # can "think before answering" — they win the reasoning family.
        low = model_id.lower()
        is_thinker = "oss" in low or low.startswith("ollama/qwen3:") or "/qwen3:" in low
        if task_family == "reasoning" and is_thinker:
            score += 1.0
        return score

    # ---------------------------------------------------------------- filters

    @staticmethod
    def _capabilities_satisfied(capabilities: ModelCapabilities, required: set[str]) -> bool:
        if not required:
            return True
        available = {
            "text_generation": capabilities.text_generation,
            "vision": capabilities.vision,
            "tool_use": capabilities.tool_use,
            "long_context": capabilities.long_context,
        }
        return required.issubset({k for k, v in available.items() if v})

    def _hardware_fits(self, model_id: str) -> bool:
        """BP §53: resource requirements vs HardwareProfile (ADR-0002)."""
        model = self._manager.get(model_id)
        requirements = model.resource_requirements
        if requirements.min_ram_mb > self._hardware.ram_mb:
            return False
        if requirements.gpu_required and not self._hardware.gpu_available:
            return False
        if requirements.min_vram_mb > self._hardware.vram_mb and requirements.gpu_required:
            return False
        return True


__all__ = ["HardwareConstraints", "ModelSelector", "SelectionCandidate"]
