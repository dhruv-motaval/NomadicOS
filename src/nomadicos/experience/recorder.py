"""Experience contracts (BP §18, §95, §206)."""

import time
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from nomadicos.core.logging import get_logger

if TYPE_CHECKING:
    from nomadicos.experience.store import ExperienceStore

logger = get_logger("experience.recorder")


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class TaskExperience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experience_id: UUID = Field(default_factory=uuid4)
    task_id: str | None = None
    session_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    outcome: Outcome
    summary: str = Field(min_length=1, max_length=4096)
    failure_class: str | None = None  # canonical taxonomy (BP §117)
    quality_score: float = Field(default=0.0, ge=0.0, le=10.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = None
    tool_calls: int = 0
    steps: int = 0


__all__ = ["ExperienceRecorder", "Outcome", "TaskExperience"]


class ExperienceRecorder:
    """Records task experiences with quality scoring (BP §95, §318)."""

    def __init__(self, store: "ExperienceStore") -> None:
        self._store = store
        self._pending_start: float | None = None

    def start(self, task_id: str) -> None:
        """BP §95: experience.start(task_id)."""
        self._pending_start = time.monotonic()

    async def record_step(self, step: dict[str, Any]) -> None:
        """BP §95: experience.record_step(...) — evidence accumulates on the
        caller side and lands in the final record."""

    async def finish(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
        outcome: Outcome,
        summary: str,
        model_id: str | None = None,
        tool_calls: int = 0,
        steps: int = 0,
        failure_class: str | None = None,
        evidence: dict[str, Any] | None = None,
        verified: bool = False,
    ) -> TaskExperience:
        """BP §95: experience.finish(...) — quality score per BP §318."""
        quality = self._quality(
            outcome=outcome,
            verified=verified,
            steps=steps,
            evidence=evidence or {},
        )
        experience = TaskExperience(
            task_id=task_id,
            session_id=session_id,
            outcome=outcome,
            summary=summary,
            failure_class=failure_class,
            quality_score=quality,
            evidence=evidence or {},
            model_id=model_id,
            tool_calls=tool_calls,
            steps=steps,
        )
        await self._store.append(experience)
        logger.info(
            "experience recorded outcome=%s quality=%s task=%s",
            outcome.value,
            quality,
            task_id,
        )
        return experience

    @staticmethod
    def _quality(
        *, outcome: Outcome, verified: bool, steps: int, evidence: dict[str, Any]
    ) -> float:
        """BP §318: success + verification strength dominate."""
        base = {"success": 8.0, "partial": 4.0, "failure": 1.0}[outcome.value]
        if verified:
            base += 2.0  # deterministic verification strength
        if evidence:
            base += 0.5  # evidence present
        if steps > 0 and outcome is Outcome.SUCCESS:
            base += 0.5  # completed through real steps
        return round(min(10.0, base), 2)

    async def consolidate(self, *, min_similar: int = 3) -> int:
        """BP §109: cluster repeated/similar experiences into a higher-level one.

        Clustering is keyword-overlap based (not exact-match) so near-duplicate
        traces that survived store-level dedup (BP §168) consolidate here.
        """
        records = await self._store.all()
        clusters: dict[str, list[TaskExperience]] = {}
        keywords_to_key: dict[frozenset[str], str] = {}

        def _keywords(text: str) -> frozenset[str]:
            import re

            words = re.findall(r"[a-z]{3,}", text.lower())
            return frozenset(words)

        for record in records:
            keywords = _keywords(record.summary)
            key = None
            for candidate_kw, candidate_key in keywords_to_key.items():
                overlap = len(keywords & candidate_kw)
                smaller = min(len(keywords), len(candidate_kw))
                if smaller and overlap / smaller >= 0.6:
                    key = candidate_key
                    break
            if key is None:
                key = f"cluster-{len(clusters)}"
                keywords_to_key[keywords] = key
            clusters.setdefault(key, []).append(record)

        consolidated = 0
        for group in clusters.values():
            if len(group) < min_similar:
                continue
            successes = sum(1 for g in group if g.outcome is Outcome.SUCCESS)
            representative = group[0].summary
            procedure = TaskExperience(
                summary=f"[consolidated ×{len(group)}] {representative}",
                outcome=Outcome.SUCCESS if successes == len(group) else Outcome.PARTIAL,
                quality_score=round(min(10.0, sum(g.quality_score for g in group) / len(group)), 2),
                evidence={"consolidated_from": [str(g.experience_id) for g in group]},
                steps=group[0].steps,
            )
            await self._remove_group(group)
            await self._store.append(procedure)
            consolidated += 1
        return consolidated

    async def _remove_group(self, group: list[TaskExperience]) -> int:
        """Remove the originals; InMemoryExperienceStore exposes its records list."""
        records_list = getattr(self._store, "records", None)
        if records_list is None:
            return 0
        ids = {g.experience_id for g in group}
        before = len(records_list)
        records_list[:] = [r for r in records_list if r.experience_id not in ids]
        return before - len(records_list)
