"""ImprovementStore + improvement contracts (BP §29, §44, §66, §101)."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nomadicos.core.errors import ImprovementRejected


class CandidateKind(StrEnum):
    """BP §66 candidate types. Model-weight changes are out of scope (Section I)."""

    MODEL_SELECTION_UPDATE = "MODEL_SELECTION_UPDATE"
    WORKFLOW_UPDATE = "WORKFLOW_UPDATE"
    PROMPT_CONFIGURATION_UPDATE = "PROMPT_CONFIGURATION_UPDATE"
    RETRIEVAL_UPDATE = "RETRIEVAL_UPDATE"
    AGENT_POLICY_UPDATE = "AGENT_POLICY_UPDATE"
    TOOL_USAGE_UPDATE = "TOOL_USAGE_UPDATE"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    SANDBOXED = "sandboxed"
    BENCHMARKED = "benchmarked"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


class ImprovementCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID = Field(default_factory=uuid4)
    kind: CandidateKind
    description: str = Field(min_length=1, max_length=2048)
    state: CandidateState = CandidateState.PROPOSED
    created_at: datetime = Field(default_factory=datetime.now)

    # The payload is the proposed *configuration delta* — never executable code.
    payload: dict[str, Any] = Field(default_factory=dict)

    # BP §67 promotion gates
    benchmark_score: float | None = Field(default=None, ge=0.0, le=10.0)
    baseline_score: float | None = Field(default=None, ge=0.0, le=10.0)
    security_passed: bool = False
    resource_cost: float | None = Field(default=None, ge=0)

    # BP §44/§45: versioning + rollback target
    version_id: UUID | None = None
    parent_version: UUID | None = None
    rollback_target: UUID | None = None
    rejection_reason: str | None = None

    @property
    def improvement_delta(self) -> float | None:
        if self.benchmark_score is None or self.baseline_score is None:
            return None
        return round(self.benchmark_score - self.baseline_score, 3)

    def assert_promotable(self, *, allow_baseline_seed: bool = False) -> None:
        """BP §67: benchmark improvement + no regressions + security + rollback.

        A baseline seed (no active version yet) trivially has a rollback path —
        restoring factory defaults (BP §359)."""
        gates: list[tuple[bool, str]] = [
            (self.benchmark_score is not None, "benchmark not run"),
            (self.baseline_score is not None, "no baseline recorded"),
            (
                self.improvement_delta is not None and self.improvement_delta > 0.0,
                "no benchmark improvement (BP §216)",
            ),
            (self.security_passed, "security check failed (BP §147)"),
            (
                allow_baseline_seed
                or self.rollback_target is not None
                or self.parent_version is not None,
                "no rollback target (BP §45)",
            ),
        ]
        failures = [msg for ok, msg in gates if not ok]
        if failures:
            raise ImprovementRejected(
                "; ".join(failures), context={"candidate": str(self.candidate_id)}
            )


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    promoted: bool
    reason: str
    decided_at: datetime = Field(default_factory=datetime.now)
    version_id: UUID | None = None

    @model_validator(mode="after")
    def _promoted_needs_version(self) -> "PromotionDecision":
        if self.promoted and self.version_id is None:
            raise ImprovementRejected("promoted candidate requires a version_id")
        return self


class ImprovementStore:
    def __init__(self) -> None:
        self.candidates: dict[UUID, ImprovementCandidate] = {}
        self.versions: dict[UUID, dict[str, Any]] = {}
        self.active_version: UUID | None = None

    async def save(self, candidate: ImprovementCandidate) -> None:
        self.candidates[candidate.candidate_id] = candidate

    async def all(self) -> list[ImprovementCandidate]:
        return list(self.candidates.values())

    async def version(self, candidate: ImprovementCandidate) -> UUID:
        """BP §44: version_id, parent_version, reason, benchmark_results,
        rollback_target — recorded at promotion."""
        version_id = uuid4()
        self.versions[version_id] = {
            "version_id": version_id,
            "parent_version": candidate.parent_version,
            "candidate_id": candidate.candidate_id,
            "kind": candidate.kind.value,
            "payload": candidate.payload,
            "benchmark_results": {
                "baseline": candidate.baseline_score,
                "candidate": candidate.benchmark_score,
                "delta": candidate.improvement_delta,
            },
            "rollback_target": candidate.rollback_target,
            "active": True,
        }
        if self.active_version is not None:
            candidate.parent_version = self.active_version
            self.versions[version_id]["parent_version"] = self.active_version
            self.versions[version_id]["rollback_target"] = self.active_version
            self.versions[self.active_version]["active"] = False
        self.active_version = version_id
        return version_id

    async def rollback_to(self, version_id: UUID) -> dict[str, Any]:
        """BP §45: restore last known-good version."""
        if version_id not in self.versions:
            raise ValueError(f"unknown version: {version_id}")
        target = self.versions[version_id]
        if self.active_version is not None:
            self.versions[self.active_version]["active"] = False
        target["active"] = True
        self.active_version = version_id
        return target

    async def active(self) -> dict[str, Any] | None:
        if self.active_version is None:
            return None
        return self.versions[self.active_version]


__all__ = ["CandidateState", "ImprovementCandidate", "ImprovementStore", "PromotionDecision"]
