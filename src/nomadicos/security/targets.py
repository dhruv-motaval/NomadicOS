"""Target registry (Sprint 9): an authorization INPUT, not a discovery
engine. Targets must be explicitly registered; matching is deterministic;
unknown/disabled/out-of-scope targets fail closed."""

from __future__ import annotations

from nomadicos.contracts.security import TargetRecord
from nomadicos.kernel.errors import InvalidProposal

MAX_TARGETS = 128


class TargetRegistry:
    def __init__(self, *, max_targets: int = MAX_TARGETS) -> None:
        if max_targets < 1:
            raise ValueError("max_targets must be >= 1")
        self._max = max_targets
        self._targets: dict[str, TargetRecord] = {}

    def register(self, target: TargetRecord) -> TargetRecord:
        """Explicit registration. Duplicates rejected — never overwritten."""
        if target.target_id in self._targets:
            raise InvalidProposal(f"duplicate target {target.target_id!r}")
        if len(self._targets) >= self._max:
            raise InvalidProposal(f"target registry full ({self._max})")
        self._targets[target.target_id] = target
        return target

    def lookup(self, target_id: str) -> TargetRecord | None:
        return self._targets.get(target_id)

    def match(self, target_id: str, engagement_id: str) -> tuple[TargetRecord | None, str]:
        """Deterministic target gate: exists, enabled, and in engagement
        scope. Returns (target, rejection_reason)."""
        target = self._targets.get(target_id)
        if target is None:
            return None, f"unknown target {target_id!r}"
        if not target.enabled:
            return None, f"target {target_id!r} is disabled"
        if target.engagement_id and target.engagement_id != engagement_id:
            return (
                None,
                f"target {target_id!r} is bound to engagement "
                f"{target.engagement_id!r}, not {engagement_id!r}",
            )
        return target, ""

    def enumerate_targets(self) -> list[TargetRecord]:
        return [self._targets[key] for key in sorted(self._targets)]

    def __len__(self) -> int:
        return len(self._targets)
