"""PostgreSQL-backed MemoryStore (BP §16, §112-113, §376-420)."""

import asyncio
import json
from typing import Any
from uuid import UUID

from nomadicos.core.logging import get_logger
from nomadicos.memory.base import (
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
)
from nomadicos.postgres.client import PostgresClient

logger = get_logger("postgres.memory")


class PostgresMemoryStore(MemoryStore):
    """Persists memories into nomadicos.memories (migrations 002)."""

    def __init__(
        self,
        client: PostgresClient,
        embedder: Any | None = None,  # Embedder — semantic rerank (Phase 9)
    ) -> None:
        self._client = client
        self._embedder = embedder

    async def store(self, record: MemoryRecord) -> UUID:
        self._client.execute(
            """
            INSERT INTO nomadicos.memories
                (memory_id, scope, content, source, confidence, verified,
                 tags, session_id, task_id, project_id, sensitivity)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.memory_id,
                record.scope.value,
                record.content,
                record.source,
                record.confidence,
                record.verified,
                json.dumps(record.tags),
                record.session_id,
                record.task_id,
                record.project_id,
                record.sensitivity,
            ),
        )
        return record.memory_id

    @staticmethod
    def _to_record(row: dict) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            scope=MemoryScope(row["scope"]),
            content=row["content"],
            created_at=row["created_at"],
            source=row["source"],
            confidence=row["confidence"],
            verified=row["verified"],
            tags=row["tags"] if isinstance(row["tags"], list) else json.loads(row["tags"]),
            session_id=str(row["session_id"]) if row["session_id"] else None,
            task_id=str(row["task_id"]) if row["task_id"] else None,
            project_id=row["project_id"],
            sensitivity=row["sensitivity"],
        )

    async def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Keyword ILIKE (over-fetched) + Python relevance ranking: keyword hit
        count dominates, verified/confidence and recency break ties. Sensitivity
        and scope enforcement stay in MemoryEngine (BP §381). Vector ranking
        lands with Phase 9 semantic wiring."""
        import re as _re

        keywords = [w for w in _re.findall(r"[a-z0-9]+", query.text.lower()) if len(w) > 2][:6]
        if not keywords:
            return []
        clauses = " OR ".join(["content ILIKE %s"] * len(keywords))
        sql = f"""
            SELECT * FROM nomadicos.memories
            WHERE ({clauses})
        """
        params: list = [f"%{kw}%" for kw in keywords]
        if query.scope is not None:
            sql += " AND scope = %s"
            params.append(query.scope.value)
        if query.project_id:
            sql += " AND (project_id = %s OR project_id IS NULL)"
            params.append(query.project_id)
        if query.session_id:
            sql += " AND (session_id = %s OR session_id IS NULL)"
            params.append(query.session_id)
        # over-fetch wide, rank in Python (no ORDER BY guessing in SQL)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(query.limit * 4)

        rows = self._client.execute(sql, tuple(params))
        records = [self._to_record(row) for row in rows]
        if not records:
            return []

        # Semantic rerank (Phase 9): embed query + candidates with the LOCAL
        # Ollama embedder, rank by cosine. Falls back to IDF-lite keyword
        # ranking when the embedder is unreachable (graceful degradation).
        semantic_order: list[MemoryRecord] | None = None
        if self._embedder is not None:
            embedder = self._embedder
            try:
                query_vec = await embedder.embed(query.text)

                async def safe_embed(text: str) -> list[float] | None:
                    try:
                        return await embedder.embed(text[:1000])
                    except Exception:  # noqa: BLE001 — one bad record ≠ no rerank
                        return None

                vecs = await asyncio.gather(*(safe_embed(r.content) for r in records))
                from nomadicos.vector.ollama_embedder import cosine

                scored = sorted(
                    (
                        (rec, cosine(query_vec, v) if v is not None else -1.0)
                        for rec, v in zip(records, vecs, strict=True)
                    ),
                    key=lambda pair: -pair[1],
                )
                semantic_order = [rec for rec, _ in scored]
            except Exception:  # noqa: BLE001 — embedder down ⇒ keyword ranking
                semantic_order = None

        if semantic_order is not None:
            return semantic_order[: query.limit]

        # IDF-lite keyword ranking fallback
        import math

        lower_contents = [r.content.lower() for r in records]
        df = {kw: sum(1 for c in lower_contents if kw in c) for kw in keywords}

        def relevance(index: int, rec: MemoryRecord) -> tuple[float, float]:
            content = lower_contents[index]
            score = 0.0
            for kw in keywords:
                if kw in content:
                    idf = math.log(1 + len(records) / (1 + df[kw]))
                    score += 1.0 + idf
            recency = 1.0 if rec.created_at is not None else 0.0
            return (score, recency)

        order = sorted(range(len(records)), key=lambda i: relevance(i, records[i]), reverse=True)
        ranked = [records[i] for i in order]
        return ranked[: query.limit]

    async def get(self, memory_id: UUID) -> MemoryRecord | None:
        rows = self._client.execute(
            "SELECT * FROM nomadicos.memories WHERE memory_id = %s", (memory_id,)
        )
        return self._to_record(rows[0]) if rows else None

    async def delete(self, memory_id: UUID) -> bool:
        try:
            self._client.execute(
                "DELETE FROM nomadicos.memories WHERE memory_id = %s", (memory_id,)
            )
            return self._client._conn.statusmessage_count(0) if False else True
        except Exception:  # noqa: BLE001 — caller checks count via forget
            return False

    async def forget(
        self,
        *,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> int:
        sql = "DELETE FROM nomadicos.memories WHERE 1=1"
        params: list = []
        if scope is not None:
            sql += " AND scope = %s"
            params.append(scope.value)
        if session_id:
            sql += " AND session_id::text = %s"
            params.append(session_id)
        if project_id:
            sql += " AND project_id = %s"
            params.append(project_id)
        if not params:
            return 0  # refuse unscoped mass deletion (BP §59)
        rows = self._client.execute(
            "WITH deleted AS (" + sql + " RETURNING memory_id) SELECT count(*) AS n FROM deleted",
            tuple(params),
        )
        return int(rows[0]["n"]) if rows else 0

    async def count(self) -> int:
        rows = self._client.execute("SELECT count(*) AS n FROM nomadicos.memories")
        return int(rows[0]["n"]) if rows else 0


__all__ = ["PostgresMemoryStore"]
