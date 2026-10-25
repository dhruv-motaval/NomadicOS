"""Sprint 5 — supersession: old facts are marked, never deleted; historical
provenance is preserved; retrieval prefers current knowledge."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
    SupersessionRecord,
)
from nomadicos.memory.retrieval import Retriever
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.supersession import SUPERSEDES, supersede


def store_at(tmp_path: Path) -> JsonlMemoryStore:
    return JsonlMemoryStore(tmp_path / "memory.jsonl")


def rec(
    rid: str,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.SEMANTIC,
    verified: bool = False,
    confidence: float = 0.6,
) -> MemoryRecord:
    return MemoryRecord(
        id=rid,
        kind=kind,
        content=content,
        provenance=MemoryProvenance(
            source_task_id="task_src", confidence=confidence, verified=verified
        ),
    )


def test_supersede_marks_old_and_keeps_both(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "auth module uses flask"))
    new = rec("mem_new", "auth module uses fastapi", verified=True, confidence=0.9)

    result = supersede(store, old_id="mem_old", new=new, reason="framework migration")

    assert result is not None
    stored_new, stored_old = result
    assert stored_new.id == "mem_new"
    assert stored_old.superseded is True
    assert stored_old.superseded_by == "mem_new"
    # historical record KEPT with full provenance (never deleted)
    ids = [r.id for r in store.records()]
    assert "mem_old" in ids and "mem_new" in ids
    assert stored_old.provenance.source_task_id == "task_src"
    assert stored_old.content == "auth module uses flask"


def test_supersede_links_graph_relation(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "old fact"))
    new = rec("mem_new", "new fact")

    result = supersede(store, old_id="mem_old", new=new, reason="updated")

    assert result is not None
    relations = store.relations()
    assert len(relations) == 1
    rel = relations[0]
    assert rel.relation == SUPERSEDES
    assert rel.source == "mem_new" and rel.target == "mem_old"
    assert "supersession" in rel.properties


def test_supersession_record_contract() -> None:
    s = SupersessionRecord(superseded_id="a", superseded_by="b", reason="why")
    assert s.superseded_id == "a" and s.superseded_by == "b"
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SupersessionRecord(superseded_id="same", superseded_by="same")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SupersessionRecord(superseded_id=" ", superseded_by="b")


def test_supersede_rejects_missing_old_cross_kind_and_self(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "old fact", kind=MemoryKind.SEMANTIC))
    assert supersede(store, old_id="mem_ghost", new=rec("mem_n", "new")) is None
    assert supersede(store, old_id="mem_old", new=rec("mem_old", "self")) is None
    assert (
        supersede(store, old_id="mem_old", new=rec("mem_e", "episode", kind=MemoryKind.EPISODIC))
        is None
    )


def test_retrieval_prefers_current_over_superseded(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "auth module uses flask"))
    supersede(store, old_id="mem_old", new=rec("mem_new", "auth module uses fastapi"))

    hits = Retriever(store).retrieve(
        MemoryQuery(text="auth module", kinds=[MemoryKind.SEMANTIC], limit=8)
    )
    assert [h.record.id for h in hits] == ["mem_new", "mem_old"]  # current first
    assert hits[0].score > hits[1].score
    assert hits[1].record.superseded is True


def test_superseded_still_retrievable_when_only_match(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "auth module uses flask"))
    supersede(store, old_id="mem_old", new=rec("mem_new", "unrelated replacement fact"))

    hits = Retriever(store).retrieve(
        MemoryQuery(text="auth module", kinds=[MemoryKind.SEMANTIC], limit=8)
    )
    assert [h.record.id for h in hits] == ["mem_old"]  # kept, still data


def test_supersession_provenance_preserved_across_restart(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("mem_old", "auth module uses flask"))
    supersede(store, old_id="mem_old", new=rec("mem_new", "auth module uses fastapi"))

    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    old = next(r for r in reloaded.records() if r.id == "mem_old")
    assert old.superseded is True
    assert old.superseded_by == "mem_new"
    assert old.provenance.source_task_id == "task_src"
    assert [r.relation for r in reloaded.relations()] == [SUPERSEDES]


def test_supersession_module_is_data_only() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.supersession", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.orchestration",
        "nomadicos.router",
        "AuthorizedAction",
        "TaskStatus",
        "subprocess",
        "Popen",
        "os.system",
    ):
        assert forbidden not in source


def test_provenance_source_type_roundtrip(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = MemoryRecord(
        id="mem_obs",
        kind=MemoryKind.SEMANTIC,
        content="watched the build finish",
        provenance=MemoryProvenance(
            source_task_id="t",
            confidence=0.7,
            source_type=MemorySource.OBSERVATION,
            evidence_refs=["exec_1", "verif_2"],
        ),
    )
    stored = store.write(record)
    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    back = next(r for r in reloaded.records() if r.id == "mem_obs")
    assert stored.provenance.source_type is MemorySource.OBSERVATION
    assert back.provenance.source_type is MemorySource.OBSERVATION
    assert back.provenance.evidence_refs == ["exec_1", "verif_2"]
