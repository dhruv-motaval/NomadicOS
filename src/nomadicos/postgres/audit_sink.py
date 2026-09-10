"""PostgreSQL-backed audit sink (BP §41-42, ADR-0020) — implements AuditSink."""

from nomadicos.audit.base import AuditEvent, AuditEventCategory, AuditSink
from nomadicos.postgres.client import PostgresClient


class PostgresAuditSink(AuditSink):
    """Append-only audit into nomadicos.audit_events (I7: no update/delete API)."""

    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def append(self, event: AuditEvent) -> None:
        import json

        self._client.execute(
            """
            INSERT INTO nomadicos.audit_events
                (event_id, category, severity, user_id, session_id, task_id,
                 run_id, step_id, subject, decision, reason, fields)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.category.value,
                event.severity.value,
                event.user_id,
                event.session_id,
                event.task_id,
                event.run_id,
                event.step_id,
                event.subject,
                event.decision,
                event.reason,
                json.dumps(event.fields),
            ),
        )

    async def query(
        self,
        *,
        category: AuditEventCategory | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        return await AuditRepositoryQuery(self._client).query(
            category=category, task_id=task_id, limit=limit
        )


class AuditRepositoryQuery:
    """Read path shared with the repository (no SQL duplication)."""

    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def query(
        self,
        *,
        category: AuditEventCategory | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        from nomadicos.postgres.repositories import AuditRepository

        return await AuditRepository(self._client).query(
            category=category, task_id=task_id, limit=limit
        )


__all__ = ["PostgresAuditSink"]
