
from nomadicos.memory.base import MemoryQuery, MemoryRecord, MemoryScope
from nomadicos.memory.fake import FakeMemoryStore


async def make_store() -> FakeMemoryStore:
    store = FakeMemoryStore()
    await store.store(
        MemoryRecord(
            scope=MemoryScope.PROJECT,
            content="Project X uses PostgreSQL 18.",
            source="session-2",
            project_id="project-x",
            verified=True,
            confidence=0.9,
        )
    )
    await store.store(
        MemoryRecord(
            scope=MemoryScope.SESSION,
            content="User asked to temporarily use directory X.",
            source="session-3",
            session_id="session-3",
        )
    )
    await store.store(
        MemoryRecord(
            scope=MemoryScope.PROJECT,
            content="Project Y uses SQLite.",
            source="session-4",
            project_id="project-y",
        )
    )
    return store


async def test_scoped_search() -> None:
    store = await make_store()
    results = await store.search(
        MemoryQuery(text="PostgreSQL", project_id="project-x")
    )
    assert len(results) == 1
    assert "PostgreSQL 18" in results[0].content


async def test_project_isolation() -> None:
    """BP §381: Project B memories are not auto-exposed to Project A."""
    store = await make_store()
    results = await store.search(
        MemoryQuery(text="SQLite database", project_id="project-x")
    )
    assert results == []


async def test_scope_filter() -> None:
    store = await make_store()
    results = await store.search(
        MemoryQuery(text="directory", scope=MemoryScope.SESSION)
    )
    assert len(results) == 1


async def test_verified_ranks_higher() -> None:
    store = FakeMemoryStore()
    await store.store(
        MemoryRecord(scope=MemoryScope.PROJECT, content="deploy with script", source="a")
    )
    await store.store(
        MemoryRecord(
            scope=MemoryScope.PROJECT,
            content="deploy with script",
            source="b",
            verified=True,
        )
    )
    results = await store.search(MemoryQuery(text="deploy script"))
    assert results[0].verified is True


async def test_forget_by_scope() -> None:
    store = await make_store()
    deleted = await store.forget(scope=MemoryScope.SESSION)
    assert deleted == 1
    assert await store.count() == 2


async def test_get_and_delete() -> None:
    store = FakeMemoryStore()
    memory_id = await store.store(
        MemoryRecord(scope=MemoryScope.USER, content="prefers concise reports", source="u")
    )
    record = await store.get(memory_id)
    assert record is not None
    assert await store.delete(memory_id) is True
    assert await store.get(memory_id) is None
