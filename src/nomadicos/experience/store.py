"""ExperienceStore: append/search/aggregate (BP §95, §109, §168, §318)."""

from abc import ABC, abstractmethod

from nomadicos.core.logging import get_logger
from nomadicos.experience.recorder import Outcome, TaskExperience

logger = get_logger("experience.store")


class ExperienceStore(ABC):
    @abstractmethod
    async def append(self, experience: TaskExperience) -> None: ...

    @abstractmethod
    async def search(
        self, query: str, *, limit: int = 5, outcome: Outcome | None = None
    ) -> list[TaskExperience]: ...

    @abstractmethod
    async def all(self) -> list[TaskExperience]: ...


class InMemoryExperienceStore(ExperienceStore):
    """Keyword-overlap search + BP §318 quality ranking; deduplication per §168."""

    def __init__(self) -> None:
        self.records: list[TaskExperience] = []

    async def append(self, experience: TaskExperience) -> None:
        # BP §168: repeated identical traces are aggregated, not stored forever.
        for existing in self.records:
            if (
                existing.summary == experience.summary
                and existing.outcome is experience.outcome
            ):
                existing.quality_score = round(
                    min(10.0, existing.quality_score + 0.5), 2
                )
                existing.steps = max(existing.steps, experience.steps)
                logger.info("experience deduplicated (aggregated) id=%s", existing.experience_id)
                return
        self.records.append(experience)

    @staticmethod
    def _keywords(text: str) -> set[str]:
        return {w.strip(".,!?;:()\"'").lower() for w in text.split() if len(w) > 2}

    async def search(
        self, query: str, *, limit: int = 5, outcome: Outcome | None = None
    ) -> list[TaskExperience]:
        keywords = self._keywords(query)
        scored: list[tuple[float, TaskExperience]] = []
        for record in self.records:
            if outcome is not None and record.outcome is not outcome:
                continue
            overlap = len(keywords & self._keywords(record.summary))
            if overlap == 0:
                continue
            score = overlap + record.quality_score / 10.0
            scored.append((score, record))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].quality_score))
        return [record for _, record in scored[:limit]]

    async def all(self) -> list[TaskExperience]:
        return list(self.records)

__all__ = ["ExperienceStore", "InMemoryExperienceStore"]
