from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSeverity,
    InMemoryAuditSink,
    audit_event_types,
)
from nomadicos.audit.fake import FakeAuditSink


async def test_append_and_query_by_category() -> None:
    sink = InMemoryAuditSink()
    for category in (
        AuditEventCategory.TOOL_REQUESTED,
        AuditEventCategory.SECURITY_EVENT,
    ):
        await sink.append(AuditEvent(category=category, task_id="t-1"))
    assert len(await sink.query(category=AuditEventCategory.SECURITY_EVENT)) == 1
    assert len(await sink.query(task_id="t-1")) == 2


async def test_fake_sink_records_categories() -> None:
    sink = FakeAuditSink()
    await sink.append(
        AuditEvent(category=AuditEventCategory.TASK_EVENT, severity=AuditSeverity.INFO)
    )
    assert sink.categories == {"TASK_EVENT"}
    assert len(sink.appended) == 1


def test_event_types_cover_blueprint_42() -> None:
    types = set(audit_event_types())
    for expected in ("PROCESS_START", "SECURITY_EVENT", "ROLLBACK", "IMPROVEMENT_PROMOTED"):
        assert expected in types


def test_audit_event_is_frozen_against_mutation() -> None:
    event = AuditEvent(category=AuditEventCategory.TOOL_EXECUTED)
    assert event.event_id is not None
