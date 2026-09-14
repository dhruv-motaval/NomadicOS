"""MemoryEngine: persistent cross-session memory facade (BP §376-420).

Combines a MemoryStore (keyword baseline; vector retrieval arrives in Phase 9)
with scope/sensitivity enforcement and provenance (BP §395, §397).

Write discipline (BP §166): not every model output becomes memory — records
require source + confidence. Sensitive scope filtering (BP §398): sensitive
memories are only returned to explicitly authorized queries.
"""

from datetime import datetime, timedelta
from typing import Any

from nomadicos.core.errors import MemoryAccessDenied
from nomadicos.core.logging import get_logger
from nomadicos.memory.base import MemoryQuery, MemoryRecord, MemoryScope, MemoryStore

logger = get_logger("memory.engine")

_KEYWORD_SCORER_WINDOW = 200


class MemoryEngine:
    """High-level memory API (BP §94): store/search/get/delete/forget/export."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # ---------------------------------------------------------------- store

    async def store(
        self,
        content: str,
        *,
        scope: MemoryScope | str,
        source: str,
        confidence: float = 0.5,
        session_id: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        sensitivity: str = "internal",
        verified: bool = False,
        tags: list[str] | None = None,
    ) -> str:
        """Store one memory. Source is mandatory (BP §166, §395)."""
        if isinstance(scope, str):
            scope = MemoryScope(scope)
        if not content.strip():
            raise MemoryAccessDenied("cannot store empty memory")
        if not source.strip():
            raise MemoryAccessDenied("memory requires a source (provenance, BP §166)")
        record = MemoryRecord(
            scope=scope,
            content=content,
            source=source,
            confidence=confidence,
            verified=verified,
            tags=tags or [],
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            sensitivity=sensitivity,  # type: ignore[arg-type]
        )
        memory_id = await self._store.store(record)
        logger.info(
            "memory stored scope=%s sensitivity=%s chars=%d",
            scope.value,
            sensitivity,
            len(content),
        )
        return str(memory_id)

    async def promote(self, memory_id: str, *, to_scope: MemoryScope, source: str) -> str:
        """Session → project/user/system promotion (BP §383): never automatic."""
        record = await self._store.get(self._as_uuid(memory_id))
        if record is None:
            raise MemoryAccessDenied(f"memory not found: {memory_id}")
        return await self.store(
            record.content,
            scope=to_scope,
            source=f"promotion:{source} (from {record.source})",
            confidence=record.confidence,
            verified=record.verified,
            tags=record.tags,
            project_id=record.project_id,
            sensitivity=record.sensitivity,
        )

    # --------------------------------------------------------------- search

    async def search(
        self,
        text: str,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        include_sensitive: bool = False,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        """Cross-session retrieval (BP §380): search spans prior sessions/projects
        subject to scope filters. Sensitive memories require explicit inclusion."""
        query = MemoryQuery(
            text=text,
            scope=scope,
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            limit=limit * 2,  # over-fetch, then sensitivity-filter
        )
        results = await self._store.search(query)
        if not include_sensitive:
            results = [r for r in results if r.sensitivity != "sensitive"]
        return results[:limit]

    async def search_beyond_session(
        self,
        text: str,
        *,
        project_id: str | None = None,
        limit: int = 5,
    ) -> list[MemoryRecord]:
        """BP §380/§412: 'use the same fix we discovered earlier' — cross-session query."""
        return await self.search(text, project_id=project_id, limit=limit)

    # ------------------------------------------------------- get/delete/forget

    async def get(self, memory_id: str) -> MemoryRecord | None:
        return await self._store.get(self._as_uuid(memory_id))

    async def delete(self, memory_id: str) -> bool:
        return await self._store.delete(self._as_uuid(memory_id))

    async def forget(
        self,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        """BP §402-403: delete raw session data / session-derived memories are
        separate operations; the caller decides which (BP §59)."""
        deleted = await self._store.forget(
            scope=scope, session_id=session_id, project_id=project_id
        )
        logger.info("memory forgotten scope=%s session=%s count=%d", scope, session_id, deleted)
        return deleted

    async def count(self) -> int:
        return await self._store.count()

    async def export(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        """BP §220: user export (JSON-serializable). Dumps all records in scope."""
        if hasattr(self._store, "records"):
            records = [
                r
                for r in self._store.records.values()
                if project_id is None or r.project_id == project_id
            ]
            return [r.model_dump(mode="json") for r in records]
        # Fallback: broad query (vector store lands in Phase 9).
        records = await self._store.search(
            MemoryQuery(text="the and or", limit=100, project_id=project_id)
        )
        return [r.model_dump(mode="json") for r in records]

    # ---------------------------------------------------------------- staleness

    @staticmethod
    def is_stale(record: MemoryRecord, *, max_age_days: float) -> bool:
        """BP §111: stale-memory detection."""
        return record.created_at < datetime.now(record.created_at.tzinfo) - timedelta(
            days=max_age_days
        )

    @staticmethod
    def _as_uuid(memory_id: str):
        import uuid

        return uuid.UUID(memory_id)


__all__ = ["MemoryEngine"]
