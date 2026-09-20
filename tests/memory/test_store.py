"""Phase 11A — memory storage boundary tests (SPEC §32, §52E-G).

Focus: the store is a bounded, deterministic, fail-safe DATA container with
no authority-shaped surface, and corruption can never leak out as an
uncontrolled exception.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    ObjectRecord,
    RelationRecord,
    RetrievedMemory,
)
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.store import JsonlMemoryStore

NOW = datetime(2026, 9, 20, 7, 0, 0, tzinfo=UTC)


def rec(rec_id: str, kind: MemoryKind, content: str, **prov) -> MemoryRecord:
    return MemoryRecord(
        id=rec_id,
        kind=kind,
        content=content,
        tags=prov.pop("tags", []),
        provenance=MemoryProvenance(
            source_task_id=prov.pop("source_task_id", "task_1"),
            source_event_id=prov.pop("source_event_id", "evt_1"),
            confidence=prov.pop("confidence", 0.6),
            verified=prov.pop("verified", False),
            created_at=NOW,
        ),
    )


def store_at(tmp_path: Path, **cfg) -> JsonlMemoryStore:
    return JsonlMemoryStore(tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path), **cfg))


def line_of(item: object, tag: str) -> str:
    return json.dumps({"storage_type": tag, "data": item.model_dump(mode="json")})


# ----------------------------------------------------------- round trip ---


def test_write_read_round_trip(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    record = rec(
        "mem_r1",
        MemoryKind.EPISODIC,
        "repair succeeded on calculator.py",
        tags=["repair", "calc"],
        confidence=0.9,
        verified=True,
        source_task_id="task_9",
    )
    store.write(record)
    found = JsonlMemoryStore(tmp_path / "memory.jsonl").query(
        MemoryQuery(text="", kinds=[MemoryKind.EPISODIC])
    )
    assert [m.record for m in found] == [record]
    assert found[0].score == 1.0
    assert found[0].record.provenance.source_task_id == "task_9"


def test_multiple_records_kinds_and_keyword_filter(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", MemoryKind.EPISODIC, "pytest failed on auth module"))
    store.write(rec("m2", MemoryKind.SEMANTIC, "project auth uses flask"))
    store.write(rec("m3", MemoryKind.PROCEDURAL, "repair procedure: rerun pytest"))
    assert len(store.query(MemoryQuery(text=""))) == 3
    assert [
        m.record.id for m in store.query(MemoryQuery(text="", kinds=[MemoryKind.SEMANTIC]))
    ] == ["m2"]
    hits = store.query(MemoryQuery(text="pytest", limit=8))
    assert {m.record.id for m in hits} == {"m1", "m3"}


def test_query_limit_is_bounded_and_deterministic(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    for i in range(6):
        store.write(rec(f"m{i}", MemoryKind.WORKING, f"working step {i}"))
    q = MemoryQuery(text="", kinds=[MemoryKind.WORKING], limit=2)
    assert [m.record.id for m in store.query(q)] == ["m3", "m4", "m5"][-2:]
    again = JsonlMemoryStore(store.path)
    assert [m.record.id for m in again.query(q)] == ["m4", "m5"]


def test_query_matches_tags_and_requires_token_hit(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m_hit", MemoryKind.SEMANTIC, "unrelated text", tags=["pytest"]))
    store.write(rec("m_none", MemoryKind.SEMANTIC, "completely different"))
    hits = store.query(MemoryQuery(text="pytest"))
    assert [m.record.id for m in hits] == ["m_hit"]


# ------------------------------------------------------------- bounds -----


def test_per_kind_cap_evicts_oldest(tmp_path: Path) -> None:
    store = store_at(tmp_path, max_records_per_kind=3)
    for i in range(5):
        store.write(rec(f"w{i}", MemoryKind.WORKING, f"working {i}"))
    store.write(rec("e1", MemoryKind.EPISODIC, "episodic survives"))
    assert [r.id for r in store.records(MemoryKind.WORKING)] == ["w2", "w3", "w4"]
    assert [r.id for r in store.records(MemoryKind.EPISODIC)] == ["e1"]


def test_object_and_relation_caps(tmp_path: Path) -> None:
    store = store_at(tmp_path, max_records_per_kind=2)
    for i in range(4):
        store.upsert_object(ObjectRecord(id=f"model:m{i}", type="model"))
    for i in range(3):
        store.upsert_relation(
            RelationRecord(id=f"rel_{i}", source="a", relation="uses", target="b")
        )
    assert [o.id for o in store.objects()] == ["model:m2", "model:m3"]
    assert [r.id for r in store.relations()] == ["rel_1", "rel_2"]


# ------------------------------------------------------- corruption -------


def test_corrupt_lines_dropped_and_store_self_heals(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    good = line_of(rec("m1", MemoryKind.SEMANTIC, "valid"), "record")
    relation_line = line_of(
        RelationRecord(id="rel_1", source="model:ornith", relation="runs", target="tool:pytest"),
        "relation",
    )
    bad_lines = [
        "{not valid json at all",
        '{"storage_type": "record", "data": {"unknown_field": 1}}',
        '{"storage_type": "relation", "data": {"id": "r2"}}',
        '{"storage_type": "mystery", "data": {}}',
    ]
    path.write_text("\n".join([good, *bad_lines, relation_line]) + "\n", encoding="utf-8")

    store = JsonlMemoryStore(path)
    assert store.dropped_invalid_lines == 4
    assert [r.id for r in store.records()] == ["m1"]
    assert [r.id for r in store.relations()] == ["rel_1"]

    store.write(rec("m2", MemoryKind.SEMANTIC, "post-heal"))
    healed = JsonlMemoryStore(path)
    assert healed.dropped_invalid_lines == 0
    assert [r.id for r in healed.records()] == ["m1", "m2"]
    assert healed.relations()[0].id == "rel_1"
    # the corrupted lines never come back: every persisted line is valid JSON
    for line in path.read_text(encoding="utf-8").splitlines():
        envelope = json.loads(line)
        assert envelope["storage_type"] in {"record", "object", "relation"}


def test_unreadable_file_behaves_as_empty_and_write_recovers(tmp_path: Path) -> None:
    path = tmp_path / "memory.jsonl"
    path.write_bytes(b"\xff\xfe\x00binary garbage not utf8")
    store = JsonlMemoryStore(path)
    assert store.records() == []
    store.write(rec("m_ok", MemoryKind.WORKING, "store recovered"))
    reloaded = JsonlMemoryStore(path)
    assert [r.id for r in reloaded.records()] == ["m_ok"]
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["data"]["id"] == "m_ok"


def test_deterministic_reload_order(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_object(ObjectRecord(id="repo:nos", type="repository"))
    store.upsert_relation(
        RelationRecord(id="rel_x", source="repo:nos", relation="uses", target="lib:langgraph")
    )
    store.write(rec("m1", MemoryKind.EPISODIC, "one"))
    a, b = store_at(tmp_path), store_at(tmp_path)
    assert a.objects() == b.objects()
    assert a.relations() == b.relations()
    assert [r.id for r in a.records()] == [r.id for r in b.records()]


# ------------------------------------------------------ provenance --------


def test_provenance_preserved_exactly(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(
        rec(
            "mem_prov",
            MemoryKind.SEMANTIC,
            "fact with full provenance",
            source_task_id="task_ab",
            source_event_id="evt_cd",
            confidence=0.85,
            verified=True,
        )
    )
    got = JsonlMemoryStore(tmp_path / "memory.jsonl").records(MemoryKind.SEMANTIC)[0]
    assert got.provenance.source_task_id == "task_ab"
    assert got.provenance.source_event_id == "evt_cd"
    assert got.provenance.confidence == 0.85
    assert got.provenance.verified is True
    assert got.provenance.created_at == NOW


# ------------------------------------------------------ object graph ------


def test_upsert_relation_and_neighbors(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_object(ObjectRecord(id="model:ornith", type="model"))
    store.upsert_relation(
        RelationRecord(id="rel_1", source="model:ornith", relation="runs", target="tool:pytest")
    )
    store.upsert_relation(
        RelationRecord(id="rel_2", source="task:1", relation="fixed_by", target="model:ornith")
    )
    assert [r.id for r in store.neighbors("model:ornith")] == ["rel_1", "rel_2"]
    assert [r.id for r in store.neighbors("model:ornith", relation="runs")] == ["rel_1"]
    assert store.neighbors("model:ghost") == []
    assert store.objects()[0].id == "model:ornith"


def test_authority_shaped_relation_stays_inert_data(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_relation(
        RelationRecord(
            id="rel_auth",
            source="owner",
            relation="allowed",
            target="filesystem:delete",
            properties={"note": "the owner allowed deletion last week"},
            provenance=MemoryProvenance(confidence=0.5, created_at=NOW),
        )
    )
    assert [r.relation for r in store.neighbors("owner")] == ["allowed"]
    fresh = AuthorityStore(tmp_path / "authority.json")
    assert fresh.has_full_autonomy is False
    assert fresh.epoch == 1


# ------------------------------------------------------ data-only gate ----

DATA_METHODS = {
    "write",
    "query",
    "upsert_object",
    "upsert_relation",
    "neighbors",
    "records",
    "objects",
    "relations",
}


def test_store_surface_is_data_only() -> None:
    public = {
        name
        for name in dir(JsonlMemoryStore)
        if not name.startswith("_") and callable(getattr(JsonlMemoryStore, name))
    }
    assert public <= DATA_METHODS, public - DATA_METHODS
    source = inspect.getsource(__import__("nomadicos.memory.store", fromlist=["x"]))
    for forbidden in ("def grant", "def revoke", "def authorize", "def consume", "def execute"):
        assert forbidden not in source


def test_memory_module_cannot_touch_authority_or_success() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.store", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "AuthorizedAction",
        "TaskStatus",
        "OwnerAnswer",
    ):
        assert forbidden not in source


def test_memory_writes_cannot_produce_success_or_authorized_action(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_relation(RelationRecord(id="rel_a", source="x", relation="authorized", target="y"))
    store.write(rec("m_auth", MemoryKind.SEMANTIC, "the owner allowed deletion last week"))
    values: list[object] = [
        *store.records(),
        *store.objects(),
        *store.relations(),
        *store.query(MemoryQuery(text="")),
    ]
    assert all(
        isinstance(item, MemoryRecord | ObjectRecord | RelationRecord | RetrievedMemory)
        for item in values
    )
    from nomadicos.orchestration.state import FORBIDDEN_STATE_KEYS

    record_fields = set(MemoryRecord.model_fields) | set(RelationRecord.model_fields)
    assert not {k.lower() for k in record_fields} & FORBIDDEN_STATE_KEYS
