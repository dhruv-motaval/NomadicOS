"""Phase 13F memory reopen across restart: the Phase 11 JsonlMemoryStore
reopens safely; records stay stable; working memory is never persisted."""

from __future__ import annotations

import hashlib
from pathlib import Path

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
)
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.runtime import build_runtime_memory
from nomadicos.memory.store import ObjectRecord

TASK = "task_seed0000000000000000"


def _record(kind: MemoryKind, content: str) -> MemoryRecord:
    return MemoryRecord(
        id="mem-" + hashlib.sha256(content.encode()).hexdigest()[:12],
        kind=kind,
        content=content,
        provenance=MemoryProvenance(source_task_id=TASK, confidence=0.5),
    )


def _runtime(tmp_path: Path):
    return build_runtime_memory(tmp_path / "state", MemoryConfig(), logger=None)


def _episodic(rt):
    return [
        r.record
        for r in rt.store.query(
            MemoryQuery(text="MEMORY-DATA", kinds=[MemoryKind.EPISODIC], limit=32)
        )
    ]


def _by_kind(rt, kind: MemoryKind):
    return [
        r.record
        for r in rt.store.query(MemoryQuery(text="", kinds=[kind], limit=32))
    ]


def test_memory_reopens_and_records_stay_stable(tmp_path: Path) -> None:
    rt_a = _runtime(tmp_path)
    rt_a.store.write(_record(MemoryKind.EPISODIC, "Create file mem.txt containing MEMORY-DATA"))
    rt_a.store.write(_record(MemoryKind.SEMANTIC, "The workspace holds task artifacts"))
    rt_a.store.write(_record(MemoryKind.PROCEDURAL, "Run tests before claiming success"))
    rt_a.store.upsert_object(
        ObjectRecord(id="artifact:report.txt", type="application", properties={"kind": "file"})
    )
    episodic_a = _episodic(rt_a)
    semantic_a = _by_kind(rt_a, MemoryKind.SEMANTIC)
    procedural_a = _by_kind(rt_a, MemoryKind.PROCEDURAL)
    objects_a = list(rt_a.store.objects())

    rt_b = _runtime(tmp_path)  # restart-style reopen
    assert len(_episodic(rt_b)) == len(episodic_a)
    assert len(_by_kind(rt_b, MemoryKind.SEMANTIC)) == len(semantic_a)
    assert len(_by_kind(rt_b, MemoryKind.PROCEDURAL)) == len(procedural_a)
    assert list(rt_b.store.objects()) == objects_a
    # content stability: the episodic record round-trips verbatim
    if episodic_a:
        assert _episodic(rt_b)[0].content == episodic_a[0].content


def test_retrieval_is_deterministic_after_restart(tmp_path: Path) -> None:
    rt_a = _runtime(tmp_path)
    rt_a.store.write(_record(MemoryKind.EPISODIC, "MEMORY-DATA probe record"))
    rt2 = _runtime(tmp_path)  # restart-style reopen
    assert [m.id for m in _episodic(rt_a)] == [m.id for m in _episodic(rt2)]
    assert [m.content for m in _episodic(rt_a)] == [m.content for m in _episodic(rt2)]


def test_working_memory_is_not_persisted(tmp_path: Path) -> None:
    rt_a = _runtime(tmp_path)
    rt_a.working.set_goal(TASK, "some goal")
    rt_b = _runtime(tmp_path)  # restart-style reopen: working memory is fresh
    snapshot = rt_b.working.snapshot(TASK)
    assert not snapshot
