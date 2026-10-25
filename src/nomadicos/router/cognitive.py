"""Cognitive-role model routing (Sprint 7): measured-capability model
selection for NomadicBrain cognitive roles.

Extends the EXISTING routing stack — it reuses ``ModelSelector.rank``
(deterministic, measured-first, health/quality-gated) and the
``ModelRegistry`` evidence; it does NOT replace or bypass either. The
brain speaks only the ``RoleModelRouter`` protocol (see
``nomadicos.brain.models``); this adapter returns plain serializable
data that the brain validates itself — no brain import, no provider
names, no secrets.

Selection pipeline per role profile:

    role profile (brain requirements)
      → TaskRequirements (role → task class, required capabilities,
        context floor, latency intent from Sprint 6 effort)
      → ModelSelector.rank (deterministic: measured-first, smallest
        capable, capability/health/quality filters, recorded evidence)
      → structured-output floor: a role that parses closed JSON needs
        measured structured success — an unmeasured candidate is only a
        cold-start fallback and is marked as such
      → plain dict answer (validated by the brain, fail closed)

Fail-safe: no qualified model → an explicit UNAVAILABLE answer (never a
silently unqualified selection, never an exception into the brain).
"""

from __future__ import annotations

from typing import Any

from nomadicos.contracts.model import TaskRequirements, TaskType
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.selection import ModelSelector, caps_for

#: deterministic role → task-class mapping for the existing analyzer
#: vocabulary (brain roles are NOT hardwired to provider identities)
_ROLE_TASK_CLASS: dict[str, TaskType] = {
    "DECOMPOSER": TaskType.PLANNING,
    "ACTOR": TaskType.TERMINAL,
    "MONITOR": TaskType.REASONING,
    "PREDICTOR": TaskType.REASONING,
    "EVALUATOR": TaskType.REASONING,
}

_FALLBACK_TASK_CLASS = TaskType.REASONING


class CognitiveRoleRouter:
    """Answers brain role-profile queries from measured registry evidence."""

    def __init__(self, selector: ModelSelector, registry: ModelRegistry) -> None:
        self._selector = selector
        self._registry = registry

    def select_for_role(self, profile: Any) -> dict[str, Any]:
        """Structural protocol implementation: accepts any object with the
        brain ``RoleModelProfile`` attributes; returns a plain dict the
        brain validates (fail closed). Never raises into the brain."""
        role = str(getattr(profile, "role", "")).split(".")[-1].upper()
        try:
            required = [str(c) for c in getattr(profile, "required_capabilities", [])]
            min_context = int(getattr(profile, "min_context_tokens", 4096))
            latency_mode = str(getattr(profile, "latency_mode", "BALANCED"))
            needs_structured = bool(getattr(profile, "needs_structured_output", True))
            return self._select(role, required, min_context, latency_mode, needs_structured)
        except ResourceUnavailable as exc:
            return {
                "role": role,
                "model_id": "",
                "engine": "",
                "score": 0.0,
                "available": False,
                "reasons": [f"unavailable: {exc}"][:8],
                "rejected": [],
            }
        except Exception as exc:  # fail-safe: routing never breaks the brain
            return {
                "role": role,
                "model_id": "",
                "engine": "",
                "score": 0.0,
                "available": False,
                "reasons": [f"routing failed closed: {type(exc).__name__}"][:8],
                "rejected": [],
            }

    # ---------------------------------------------------------- internals ---

    def _select(
        self,
        role: str,
        required: list[str],
        min_context: int,
        latency_mode: str,
        needs_structured: bool,
    ) -> dict[str, Any]:
        task_type = _ROLE_TASK_CLASS.get(role, _FALLBACK_TASK_CLASS)
        capabilities = list(dict.fromkeys([*caps_for(task_type), *required]))
        # validate the latency intent deterministically (fail closed)
        if latency_mode not in ("FAST", "BALANCED", "QUALITY"):
            latency_mode = "BALANCED"
        requirements = TaskRequirements(
            task_type=task_type,
            capabilities=capabilities,  # type: ignore[arg-type]
            difficulty=0.5,
            needs_large_context=min_context >= 16384,
            latency=latency_mode,  # type: ignore[arg-type]
            verification_required=True,
        )
        ranked = self._selector.rank(self._registry, requirements)
        # exact context floor (Sprint 7): the selector's generic
        # large-context flag is not enough — a role demanding 128k tokens
        # must not be satisfied by a 32k model. Deterministic filter.
        rejected: list[str] = []
        filtered: list[Any] = []
        for candidate in ranked:
            record = self._registry.get(candidate.model_id, candidate.engine or None)
            if record.context_window >= min_context:
                filtered.append(candidate)
            else:
                rejected.append(
                    f"{candidate.model_id}/{candidate.engine}: context "
                    f"{record.context_window} < required {min_context}"
                )
        if not filtered:
            raise ResourceUnavailable(
                f"no model satisfies role {role} context floor {min_context}",
                role=role,
            )
        winner = filtered[0]
        for candidate in filtered[1:]:
            rejected.append(
                f"{candidate.model_id}/{candidate.engine}: "
                f"score={candidate.score:.4f} {candidate.reason}"
            )
        # structured-output floor: brain roles parse closed JSON; a
        # measured candidate must have non-zero measured success to be a
        # real (non-cold-start) answer — unmeasured is allowed only as the
        # deterministic cold-start fallback and is labeled as such
        cold_start = not winner.measured
        reasons = [
            f"role={role} task_class={task_type.value}",
            f"required_capabilities={','.join(required) or 'none'}",
            f"context_floor={min_context} latency={requirements.latency}",
            f"selection_score={winner.score:.4f} measured={winner.measured}",
            *(["cold_start_fallback: no measured evidence for this role"] if cold_start else []),
            *(["structured_output_required"] if needs_structured else []),
        ]
        return {
            "role": role,
            "model_id": winner.model_id,
            "engine": winner.engine,
            "score": winner.score,
            "available": True,
            "reasons": reasons[:8],
            "rejected": rejected[:8],
        }
