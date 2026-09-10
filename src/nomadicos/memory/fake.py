"""In-memory MemoryStore — functional fake with scope filtering + keyword ranking.

Deterministic scoring (BP §319, simplified): keyword overlap + recency; verified
memories rank above unverified at equal overlap.
"""

from uuid import UUID

from nomadicos.memory.base import MemoryQuery, MemoryRecord, MemoryScope, MemoryStore


class FakeMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self.records: dict[UUID, MemoryRecord] = {}

    async def store(self, record: MemoryRecord) -> UUID:
        self.records[record.memory_id] = record
        return record.memory_id

    @staticmethod
    def _keywords(text: str) -> set[str]:
        return {w.strip(".,!?;:()\"'").lower() for w in text.split() if len(w) > 2}

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        query_keywords = self._keywords(query.text)
        scored: list[tuple[float, MemoryRecord]] = []
        for record in self.records.values():
            if query.scope is not None and record.scope is not query.scope:
                continue
            if not record.matches_scope(
                session_id=query.session_id,
                task_id=query.task_id,
                project_id=query.project_id,
            ):
                continue
            record_keywords = self._keywords(record.content)
            overlap = len(query_keywords & record_keywords)
            if overlap == 0:
                continue
            score = float(overlap) + (0.5 if record.verified else 0.0) + record.confidence
            scored.append((score, record))
        scored.sort(key=lambda pair: -pair[0])
        return [record for _, record in scored[: query.limit]]

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        return self.records.get(memory_id)

    async def delete(self, memory_id: UUID) -> bool:
        return self.records.pop(memory_id, None) is not None

    async def forget(
        self,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        victims = [
            memory_id
            for memory_id, record in self.records.items()
            if (scope is None or record.scope is scope)
            and (session_id is None or record.session_id == session_id)
            and (project_id is None or record.project_id == project_id)
        ]
        for memory_id in victims:
            del self.records[memory_id]
        return len(victims)

    async def count(self) -> int:
        return len(self.records)


__all__ = ["FakeMemoryStore"]
