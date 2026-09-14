"""Phase 8 tests: memory engine — scopes, promotion, cross-session, staleness."""

import pytest

from nomadicos.core.errors import MemoryAccessDenied
from nomadicos.memory.base import MemoryScope
from nomadicos.memory.engine import MemoryEngine
from nomadicos.memory.fake import FakeMemoryStore


@pytest.fixture()
def engine() -> MemoryEngine:
    return MemoryEngine(FakeMemoryStore())


async def test_store_requires_source_and_content(engine: MemoryEngine) -> None:
    """BP §166: provenance is mandatory for memory writes."""
    with pytest.raises(MemoryAccessDenied, match="provenance"):
        await engine.store("x", scope=MemoryScope.PROJECT, source="  ")
    with pytest.raises(MemoryAccessDenied, match="empty"):
        await engine.store("   ", scope=MemoryScope.PROJECT, source="s")


async def test_cross_session_retrieval(engine: MemoryEngine) -> None:
    """BP §380/§408: Session 5 can retrieve Session 2 knowledge."""
    await engine.store(
        "Project X uses PostgreSQL 18.",
        scope=MemoryScope.PROJECT,
        source="session-2",
        project_id="project-x",
        verified=True,
    )
    results = await engine.search_beyond_session("PostgreSQL", project_id="project-x")
    assert len(results) == 1
    assert results[0].source == "session-2"  # provenance retained


async def test_project_isolation(engine: MemoryEngine) -> None:
    """BP §381: Project B memories not auto-exposed to Project A."""
    await engine.store(
        "Project Y uses SQLite.",
        scope=MemoryScope.PROJECT,
        source="session-4",
        project_id="project-y",
    )
    results = await engine.search_beyond_session("SQLite", project_id="project-x")
    assert results == []


async def test_sensitive_requires_explicit_inclusion(engine: MemoryEngine) -> None:
    await engine.store(
        "API key material placeholder",
        scope=MemoryScope.USER,
        source="user",
        sensitivity="sensitive",
    )
    default = await engine.search("API key")
    assert default == []  # BP §398: sensitive hidden by default
    explicit = await engine.search("API key", include_sensitive=True)
    assert len(explicit) == 1


async def test_promotion_is_explicit(engine: MemoryEngine) -> None:
    """BP §383: session → project promotion is never automatic."""
    memory_id = await engine.store(
        "We decided to use uv as the package manager.",
        scope=MemoryScope.SESSION,
        source="session-3",
        session_id="session-3",
    )
    promoted_id = await engine.promote(memory_id, to_scope=MemoryScope.PROJECT, source="user")
    promoted = await engine.get(promoted_id)
    assert promoted is not None
    assert promoted.scope is MemoryScope.PROJECT
    assert "promotion" in promoted.source


async def test_session_forget(engine: MemoryEngine) -> None:
    """BP §402-403: forget this session / derived memories are separate ops."""
    await engine.store(
        "temp note",
        scope=MemoryScope.SESSION,
        source="session-9",
        session_id="session-9",
    )
    await engine.store("permanent note", scope=MemoryScope.PROJECT, source="session-9")
    deleted = await engine.forget(session_id="session-9", scope=MemoryScope.SESSION)
    assert deleted == 1
    assert await engine.count() == 1


async def test_staleness_detection(engine: MemoryEngine) -> None:
    """BP §111: old memories flagged for re-verification."""
    from datetime import UTC, datetime, timedelta

    memory_id = await engine.store(
        "Current package version info",
        scope=MemoryScope.PROJECT,
        source="session-1",
    )
    record = await engine.get(memory_id)
    assert record is not None
    assert MemoryEngine.is_stale(record, max_age_days=30) is False
    # simulate old memory
    old_record = record.model_copy(update={"created_at": datetime.now(UTC) - timedelta(days=60)})
    assert MemoryEngine.is_stale(old_record, max_age_days=30) is True


async def test_export_is_json_safe(engine: MemoryEngine) -> None:
    import json

    await engine.store("exportable fact", scope=MemoryScope.PROJECT, source="s1")
    exported = await engine.export()
    assert len(exported) >= 1
    json.dumps(exported)  # must not raise (BP §220: portable formats)
