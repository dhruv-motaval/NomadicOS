"""Recording audit sink — mirrors appends into an in-memory list and asserts
append-only contract during tests (ADR-0020)."""

from nomadicos.audit.base import AuditEvent, AuditSink


class FakeAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.appended: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)
        self.appended.append(event)

    async def query(
        self,
        *,
        category=None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        selected = [
            e
            for e in self.events
            if (category is None or e.category is category)
            and (task_id is None or e.task_id == task_id)
        ]
        return selected[-limit:]

    @property
    def categories(self) -> set[str]:
        return {e.category.value for e in self.events}


__all__ = ["FakeAuditSink"]
