"""LearningEngine: the self-improvement loop (BP §29-31, §101; answers Section I).

propose → sandbox → benchmark → compare → promote/reject → version → monitor
→ rollback. Evidence-gated: a candidate "looks better" is never enough
(BP §67). All local (BP §65). Candidates are configuration deltas, never
executable payloads (BP §66; invariant I4).
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from nomadicos.constitution.invariants import validate_policy_against_invariants
from nomadicos.core.errors import ImprovementRejected
from nomadicos.core.logging import get_logger
from nomadicos.learning.store import (
    CandidateKind,
    CandidateState,
    ImprovementCandidate,
    PromotionDecision,
)

logger = get_logger("learning.engine")


class LearningEngine:
    """Owns the improvement lifecycle. Fail-closed: any gate failure rejects."""

    def __init__(self, store: Any) -> None:
        self._store = store  # ImprovementStore
        self._emergency_stopped = False

    # ------------------------------------------------------------------ propose

    async def propose(
        self,
        kind: CandidateKind,
        description: str,
        payload: dict[str, Any],
        *,
        source: str,
    ) -> ImprovementCandidate:
        """BP §65: experiences → pattern → proposal. The payload is validated
        against the Constitution (I4): it may not grant privileged capabilities."""
        validate_policy_against_invariants(payload)  # raises on violation
        candidate = ImprovementCandidate(kind=kind, description=description, payload=payload)
        await self._store.save(candidate)
        logger.info(
            "improvement proposed id=%s kind=%s source=%s",
            candidate.candidate_id,
            kind.value,
            source,
        )
        return candidate

    # ----------------------------------------------------------------- sandbox

    async def run_sandboxed(
        self,
        candidate: ImprovementCandidate,
        sandbox_fn: Callable[[dict[str, Any]], Any],
    ) -> ImprovementCandidate:
        """BP §147: candidates are executed in the sandbox, never live."""
        if self._emergency_stopped:
            raise ImprovementRejected("learning paused (emergency stop)")
        started = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: sandbox_fn(candidate.payload))
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            candidate.state = CandidateState.REJECTED
            candidate.rejection_reason = f"sandbox execution failed: {exc}"
            await self._store.save(candidate)
            raise ImprovementRejected(candidate.rejection_reason) from exc
        candidate.state = CandidateState.SANDBOXED
        logger.info(
            "candidate sandboxed id=%s latency_ms=%.1f",
            candidate.candidate_id,
            (time.monotonic() - started) * 1000,
        )
        await self._store.save(candidate)
        return candidate

    # --------------------------------------------------------------- benchmark

    async def benchmark(
        self,
        candidate: ImprovementCandidate,
        *,
        baseline_fn: Callable[[], float],
        candidate_fn: Callable[[], float],
        security_check: Callable[[ImprovementCandidate], bool] | None = None,
    ) -> ImprovementCandidate:
        """BP §67/§216: candidate must beat the baseline on the same benchmark."""
        candidate.baseline_score = round(baseline_fn(), 3)
        candidate.benchmark_score = round(candidate_fn(), 3)
        candidate.security_passed = security_check(candidate) if security_check else True
        candidate.state = CandidateState.BENCHMARKED
        await self._store.save(candidate)
        delta = candidate.improvement_delta
        logger.info(
            "candidate benchmarked id=%s baseline=%s candidate=%s delta=%s",
            candidate.candidate_id,
            candidate.baseline_score,
            candidate.benchmark_score,
            delta,
        )
        return candidate

    # ------------------------------------------------------- promote / reject

    async def decide(self, candidate: ImprovementCandidate) -> PromotionDecision:
        """BP §67 gates: improvement + no regression + security + rollback path.

        The first promotion (no active version) is a baseline seed: rollback to
        factory defaults is trivially available (BP §359).
        """
        active = await self._store.active()
        if active is not None:
            candidate.rollback_target = active["version_id"]
        try:
            candidate.assert_promotable(allow_baseline_seed=active is None)
        except ImprovementRejected as exc:
            candidate.state = CandidateState.REJECTED
            candidate.rejection_reason = str(exc)
            await self._store.save(candidate)
            logger.warning(
                "improvement rejected id=%s reason=%s", candidate.candidate_id, exc
            )
            return PromotionDecision(
                candidate_id=candidate.candidate_id, promoted=False, reason=str(exc)
            )

        version_id = await self._store.version(candidate)
        candidate.state = CandidateState.PROMOTED
        candidate.version_id = version_id
        candidate.rollback_target = candidate.parent_version
        await self._store.save(candidate)
        logger.info("improvement promoted id=%s version=%s", candidate.candidate_id, version_id)
        return PromotionDecision(
            candidate_id=candidate.candidate_id,
            promoted=True,
            reason=f"delta {candidate.improvement_delta} with security pass",
            version_id=version_id,
        )

    async def reject(
        self, candidate: ImprovementCandidate, reason: str
    ) -> PromotionDecision:
        candidate.state = CandidateState.REJECTED
        candidate.rejection_reason = reason
        await self._store.save(candidate)
        logger.warning("improvement rejected id=%s reason=%s", candidate.candidate_id, reason)
        return PromotionDecision(
            candidate_id=candidate.candidate_id, promoted=False, reason=reason
        )

    # ----------------------------------------------------------------- rollback

    async def rollback(self, version_id: Any) -> Any:
        """BP §45/§289: regression ⇒ restore the version's rollback target
        (the last known-good version)."""
        for candidate in await self._store.all():
            if candidate.version_id == version_id and candidate.state.value == "promoted":
                candidate.state = CandidateState.ROLLED_BACK
                await self._store.save(candidate)
        target = self._store.versions.get(version_id)
        restore_id = target.get("rollback_target") if target else None
        if restore_id is None:
            raise ValueError(f"version {version_id} has no rollback target")
        restored = await self._store.rollback_to(restore_id)
        logger.warning(
            "improvement rolled back version=%s restored=%s", version_id, restored["version_id"]
        )
        return restored

    async def monitor(self, version_id: Any, check_fn: Callable[[], bool]) -> bool:
        """BP §44 monitor: post-promotion health check; failure ⇒ rollback flag."""
        healthy = check_fn()
        if not healthy:
            logger.warning("monitoring detected regression version=%s", version_id)
        return healthy

    def pause_learning(self) -> None:
        """BP §245: foreground work has priority (resource pressure pause)."""
        self._emergency_stopped = True

    def resume_learning(self) -> None:
        self._emergency_stopped = False
