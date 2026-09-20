"""Phase 11E — object/entity graph tests.

Deterministic depth-1 graph over the MemoryStore boundary: identity,
upsert semantics, neighbor ordering, bounds, dangling relations, and the
11A inert-DATA security precedent for authority-shaped relations.
"""

from __future__ import annotations

import inspect

from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.memory import ObjectRecord, RelationRecord
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.graph import ObjectGraph, object_id, relation_id
from nomadicos.memory.store import JsonlMemoryStore


def graph_at(tmp_path, **cfg) -> ObjectGraph:
    return ObjectGraph(
        JsonlMemoryStore(
            tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path), **cfg)
        )
    )


def rel(source: str, relation: str, target: str) -> RelationRecord:
    return RelationRecord(id="", source=source, relation=relation, target=target)


def test_object_upsert_and_get(tmp_path) -> None:
    g = graph_at(tmp_path)
    created = g.upsert_object(
        ObjectRecord(id="model:ornith", type="model", properties={"roles": "worker"})
    )
    assert created.id == "model:ornith"
    found = g.get_object("model:ornith")
    assert found is not None and found.type == "model"
    assert found.properties == {"roles": "worker"}
    assert g.get_object("model:ghost") is None
    big = g.upsert_object(
        ObjectRecord(
            id="repo:nos",
            type="repository",
            properties={f"k{i}": "v" * 200 for i in range(12)},
        )
    )
    assert len(big.properties) <= 8  # bounded metadata
    assert all(len(v) <= 120 for v in big.properties.values())


def test_object_deterministic_identity() -> None:
    assert object_id("model", "ornith") == "model:ornith"
    assert object_id("Model ", " Ornith ") == object_id("model", "ornith")


def test_object_upsert_replaces_without_duplicate(tmp_path) -> None:
    g = graph_at(tmp_path)
    g.upsert_object(ObjectRecord(id="repo:nos", type="repository"))
    updated = g.upsert_object(
        ObjectRecord(id="repo:nos", type="repository", properties={"lang": "python"})
    )
    assert updated.properties == {"lang": "python"}
    objs = g.find_objects()
    assert len(objs) == 1 and objs[0].id == "repo:nos"  # replaced in place


def test_relation_deterministic_identity_and_upsert(tmp_path) -> None:
    g = graph_at(tmp_path)
    a = g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
    b = g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
    assert a.id == b.id == relation_id("model:ornith", "runs", "tool:pytest")
    assert len(g.relations()) == 1  # same triple -> same id -> replace
    updated = g.upsert_relation(
        RelationRecord(
            id=a.id,
            source="model:ornith",
            relation="runs",
            target="tool:pytest",
            properties={"v": 2},
        )
    )
    assert updated.properties == {"v": "2"}  # bounded metadata is stringified
    assert len(g.relations()) == 1  # replaced, not duplicated
    other = g.upsert_relation(rel("model:ornith", "runs", "tool:ruff"))
    assert other.id != a.id


def test_relation_replacement_keeps_order(tmp_path) -> None:
    g = graph_at(tmp_path)
    g.upsert_relation(rel("a", "uses", "b"))
    g.upsert_relation(rel("b", "uses", "c"))
    first = g.get_relation(relation_id("a", "uses", "b"))
    assert first is not None
    g.upsert_relation(RelationRecord(id=first.id, source="a", relation="runs", target="b"))
    rels = g.relations()
    assert [r.id for r in rels] == [
        relation_id("a", "uses", "b"),
        relation_id("b", "uses", "c"),
    ]
    assert rels[0].relation == "runs"  # replaced in place, order stable


def test_neighbors_filtered_and_incoming(tmp_path) -> None:
    g = graph_at(tmp_path)
    for oid, kind in (("model:ornith", "model"), ("tool:pytest", "tool")):
        g.upsert_object(ObjectRecord(id=oid, type=kind))
    g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
    g.upsert_relation(rel("task:1", "fixed_by", "model:ornith"))
    assert [r.relation for r in g.neighbors("model:ornith", relation="runs")] == ["runs"]
    assert [r.relation for r in g.neighbors("tool:pytest", relation="runs")] == ["runs"]
    assert g.neighbors("model:ghost") == []
    assert g.adjacent("model:ghost") == []


def test_neighbor_ordering_deterministic_across_reload(tmp_path) -> None:
    g = graph_at(tmp_path)
    g.upsert_object(ObjectRecord(id="model:ornith", type="model"))
    g.upsert_relation(rel("model:ornith", "uses", "lib:langgraph"))
    g.upsert_relation(rel("task:1", "fixed_by", "model:ornith"))
    g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
    neighbors = g.neighbors("model:ornith")
    expected = [(r.relation, r.id) for r in neighbors]
    assert expected == sorted(expected)  # explicit (relation, id) ordering
    assert [r.relation for r in g.neighbors("model:ornith")] == [
        "fixed_by",
        "runs",
        "uses",
    ]
    reloaded = graph_at(tmp_path)
    assert [(r.relation, r.id) for r in reloaded.neighbors("model:ornith")] == expected


def test_depth1_traversal_boundary(tmp_path) -> None:
    g = graph_at(tmp_path)
    for oid in ("a", "b", "c"):
        g.upsert_object(ObjectRecord(id=oid, type="thing"))
    g.upsert_relation(rel("a", "uses", "b"))
    g.upsert_relation(rel("b", "uses", "c"))
    assert [r.id for r in g.neighbors("a")] == [relation_id("a", "uses", "b")]
    assert all(r.target != "c" for r in g.neighbors("a"))  # depth-1 only
    assert [o.id for o in g.adjacent("a")] == ["b"]  # C is not adjacent to A


def test_dangling_relations_allowed_and_deterministic(tmp_path) -> None:
    g = graph_at(tmp_path)
    g.upsert_object(ObjectRecord(id="model:ornith", type="model"))
    # relations to unknown objects are stored as plain data (no FK model)
    dangling = g.upsert_relation(rel("model:ornith", "uses", "lib:ghost"))
    assert g.get_relation(dangling.id) is not None
    assert [r.relation for r in g.neighbors("model:ornith")] == ["uses"]
    assert g.adjacent("model:ornith") == []  # missing endpoint skipped
    assert [r.id for r in graph_at(tmp_path).neighbors("model:ornith")] == [dangling.id]


def test_bounds_per_collection_cap(tmp_path) -> None:
    g = graph_at(tmp_path, max_records_per_kind=2)
    for i in range(3):
        g.upsert_object(ObjectRecord(id=f"model:m{i}", type="model"))
    assert [o.id for o in g.find_objects()] == ["model:m1", "model:m2"]
    for i in range(3):
        g.upsert_relation(rel("t", "uses", f"t{i}"))
    assert [r.target for r in g.relations()] == ["t1", "t2"]


def test_authority_shaped_relations_stay_inert(tmp_path) -> None:
    g = graph_at(tmp_path)
    names = ("allows", "permission", "owner", "execute", "deny", "policy")
    for name in names:
        g.upsert_relation(rel("thing:x", name, "thing:y"))
    assert {r.relation for r in g.relations()} == set(names)  # inert DATA
    fresh = AuthorityStore(tmp_path / "authority.json")
    assert fresh.has_full_autonomy is False  # memory never touches authority
    assert fresh.epoch == 1


def test_repeated_identical_operations_are_deterministic(tmp_path) -> None:
    def run_ops(g: ObjectGraph) -> tuple[list[str], list[tuple[str, str]]]:
        g.upsert_object(ObjectRecord(id="model:ornith", type="model"))
        g.upsert_object(ObjectRecord(id="tool:pytest", type="tool"))
        g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
        g.upsert_relation(rel("model:ornith", "uses", "lib:langgraph"))
        g.upsert_object(
            ObjectRecord(id="model:ornith", type="model", properties={"v": 2})
        )
        neighbors = [(r.relation, r.id) for r in g.neighbors("model:ornith")]
        objs = [o.id for o in g.find_objects()]
        return neighbors, objs

    a, b = graph_at(tmp_path), graph_at(tmp_path)
    assert run_ops(a) == run_ops(a)  # repeated ops on one graph are stable
    assert run_ops(a) == run_ops(b)  # two graphs agree on identical content


def test_reload_preserves_graph_state(tmp_path) -> None:
    g = graph_at(tmp_path)
    g.upsert_object(ObjectRecord(id="model:ornith", type="model", properties={"v": "1"}))
    g.upsert_object(ObjectRecord(id="tool:pytest", type="tool"))
    g.upsert_relation(rel("model:ornith", "runs", "tool:pytest"))
    reloaded = graph_at(tmp_path)
    assert [o.id for o in reloaded.find_objects()] == ["model:ornith", "tool:pytest"]
    assert [(r.relation, r.id) for r in reloaded.relations()] == [
        (r.relation, r.id) for r in g.relations()
    ]
    assert reloaded.get_object("model:ornith") is not None


# ------------------------------------------------------ data-only guards ---


def test_no_authority_paths_in_graph_module() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.graph", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.orchestration",
        "nomadicos.router",
        "import langgraph",
        "AuthorizedAction",
        "TaskStatus",
        "SUCCESS",
        "def write(",
        "subprocess",
        "Popen",
        "os.system",
        "interrupt(",
        "execute(",
    ):
        assert forbidden not in source
