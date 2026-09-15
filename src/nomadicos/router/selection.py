"""Model selection: deterministic pipeline ending in the smallest capable
model that satisfies the task (SPEC §13, §52B, §56.11-12).

    candidates -+-> capability match -> health/resource filter ->
                quality+historical gate -> size-minimal selection
"""

from __future__ import annotations

from pydantic import Field

from nomadicos.contracts.core import Contract
from nomadicos.contracts.model import (
    CapabilityTag,
    ModelHealth,
    ModelRecord,
    TaskRequirements,
    TaskType,
)
from nomadicos.kernel.config import RouterConfig
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.registry.scanner import estimate_size_b

#: task types whose routing mode still requires the strongest critic role.
CRITIC_TASKS = {TaskType.REASONING}

_PREFERRED_ROLES: dict[TaskType, list[str]] = {
    TaskType.CLASSIFICATION: ["tiny", "worker"],
    TaskType.CHAT: ["worker", "tiny"],
    TaskType.SIMPLE_FILE: ["worker", "tiny"],
    TaskType.TERMINAL: ["worker"],
    TaskType.CODING: ["coding", "worker", "reasoning"],
    TaskType.TESTING: ["coding", "worker", "evaluator"],
    TaskType.REASONING: ["reasoning", "critic"],
    TaskType.PLANNING: ["reasoning", "worker"],
}

_SIZELESS_MODEL = 7.0  # neutral size proxy when no hint exists


class CandidateScore(Contract):
    model_id: str
    stage: str = "qualified"
    score: float
    size_b: float
    measured: bool
    reason: str = ""


class SelectionResult(Contract):
    selected: ModelRecord
    requirements_key: str
    ranked: list[CandidateScore] = Field(default_factory=list)


def _size_proxy(record: ModelRecord) -> float:
    hint = record.params.get("size_hint_b")
    if isinstance(hint, (int, float)):
        return float(hint)
    return estimate_size_b(record.model_id) or _SIZELESS_MODEL


def _quality_gate(router: RouterConfig) -> float:
    return router.minimum_quality.get(router.mode, 0.7)


class ModelSelector:
    def __init__(self, router: RouterConfig) -> None:
        self._router = router

    def rank(self, registry: ModelRegistry, requirements: TaskRequirements) -> list[CandidateScore]:
        task_class = requirements.task_type.value
        preferred = _PREFERRED_ROLES.get(requirements.task_type, ["worker"])
        min_quality = _quality_gate(self._router) if requirements.verification_required else 0.0
        ranked: list[CandidateScore] = []
        for record in registry.enabled():
            if record.health in (ModelHealth.UNHEALTHY,):
                continue  # health/resource filter
            if not set(requirements.capabilities) <= set(record.capabilities):
                continue  # capability filter removes non-tool models (§13)
            if requirements.needs_large_context and record.context_window < 16384:
                continue
            bundle = registry.bundle_for(record.model_id, task_class)
            measured = bundle.samples > 0 and bundle.success_rate is not None
            success = bundle.success_rate if measured else None
            role_fit = max(
                (1.0 / (preferred.index(r) + 1) for r in record.roles if r in preferred),
                default=0.4,
            )
            score = (
                role_fit
                if success is None
                else (
                    (1 - self._router.historical_weight) * role_fit
                    + self._router.historical_weight * success
                )
            )
            if success is not None and success < min_quality:
                continue  # quality gate on measurements
            if success is None and min_quality > 1.0:
                continue
            ranked.append(
                CandidateScore(
                    model_id=record.model_id,
                    score=round(score, 4),
                    size_b=_size_proxy(record),
                    measured=measured,
                    reason=f"role_fit={role_fit:.2f} success={success}",
                )
            )
        # unmeasured candidates only serve cold start; measured ones outrank them
        ranked.sort(key=lambda c: (not c.measured, c.size_b, -c.score, c.model_id))
        return ranked

    def select(self, registry: ModelRegistry, requirements: TaskRequirements) -> SelectionResult:
        ranked = self.rank(registry, requirements)
        measured_qualified = [c for c in ranked if c.measured]
        pool = measured_qualified or ranked
        if not pool:
            raise ResourceUnavailable(
                "no model satisfies task capabilities/health (SPEC §13)",
                task_class=requirements.task_type.value,
            )
        # smallest capable model first (§56.11): pool is sorted by qualification.
        winner = pool[0]
        return SelectionResult(
            selected=registry.get(winner.model_id),
            requirements_key=requirements.task_type.value,
            ranked=ranked,
        )

    def select_critic(self, registry: ModelRegistry) -> ModelRecord:
        """Strongest specialist for review — never the default worker route."""
        critics = [
            r
            for r in registry.enabled()
            if ("critic" in r.roles or "evaluator" in r.roles or "reasoning" in r.roles)
            and r.health is not ModelHealth.UNHEALTHY
        ]
        if not critics:
            raise ResourceUnavailable("no critic-capable model registered")
        critics.sort(key=lambda r: (-_size_proxy(r), r.model_id))  # strongest plausible first
        return critics[0]

    def select_worker_for_class(self, registry: ModelRegistry, task_class: str) -> ModelRecord:
        """Used by benchmarks/agents needing a specific class' best measured."""
        try:
            req_type = TaskType(task_class)
        except ValueError:
            req_type = TaskType.CODING
        reqs = TaskRequirements(
            task_type=req_type,
            capabilities=caps_for(req_type),
            difficulty=0.5,
        )
        return self.select(registry, reqs).selected


def caps_for(task_type: TaskType) -> list[CapabilityTag]:
    if task_type in (TaskType.CODING, TaskType.TESTING):
        return [CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.CODING]
    if task_type in (TaskType.SIMPLE_FILE, TaskType.TERMINAL):
        return [CapabilityTag.TEXT, CapabilityTag.TOOL_USE]
    return [CapabilityTag.TEXT]
