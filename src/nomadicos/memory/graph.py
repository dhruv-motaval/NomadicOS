"""Object / entity memory graph (SPEC §32, §52E; Phase 11E) — DATA only.

A durable, deterministic DEPTH-1 graph of typed objects and inert relations
over the existing MemoryStore boundary. Graph data is untrusted DATA:

- authority-shaped relation names (allowed/permission/owner/policy/...)
  are stored as inert text and never acquire execution meaning (11A
  security precedent); the module imports no authority/executor/routing
  code, mints no execution artifacts, executes no tools, records no
  task outcome, and touches no LangGraph state;
- identity is deterministic: objects are ``<type>:<name>`` (the SPEC §52E
  ``model:ornith`` shape); relations are content-addressed
  ``rel-<sha16(source|relation|target)>`` — never random UUIDs;
- upserts replace by id (no duplicates, no silent merging of distinct
  objects); dangling relations are allowed — the contract enforces no
  referential integrity, so behavior stays simple and deterministic;
- neighbors/adjacent are DEPTH-1 only (no multi-hop search, no ranking,
  no embeddings), ordered explicitly by (relation type, relation id);
- bounded metadata: properties are capped (keys, value chars) before
  storage; graph size is bounded by the store's per-collection caps; the
  graph object itself is a stateless façade — no duplicate in-memory
  cache of the durable store.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from nomadicos.contracts.memory import ObjectRecord, RelationRecord
from nomadicos.memory.store import MemoryStore

_MAX_PROPS = 8
_PROP_KEY_CHARS = 48
_PROP_VALUE_CHARS = 120


def object_id(kind: str, name: str) -> str:
    """Deterministic SPEC §52E-style object identity: ``<type>:<name>``."""
    kind_n = " ".join(kind.split()).lower()
    name_n = " ".join(name.split()).lower()
    return f"{kind_n}:{name_n}"[:120]


def relation_id(source: str, relation: str, target: str) -> str:
    """Deterministic relation identity from its semantic triple."""
    digest = hashlib.sha256(f"{source}|{relation}|{target}".encode()).hexdigest()[:16]
    return f"rel-{digest}"


def _bounded(properties: Mapping[str, object]) -> dict[str, str]:
    """Deterministic bounded metadata: sorted keys, capped sizes."""
    out: dict[str, str] = {}
    for key in sorted(properties, key=str)[:_MAX_PROPS]:
        value = properties[key]
        if value is None:
            continue
        bounded_value = str(value)[:_PROP_VALUE_CHARS]
        if bounded_value:
            out[str(key)[:_PROP_KEY_CHARS]] = bounded_value
    return out


class ObjectGraph:
    """Depth-1 object/relation façade over MemoryStore graph records.

    Statelessly delegates to the store (no cache), keeps deterministic
    ordering, and treats every record as inert DATA.
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # ----------------------------------------------------------- objects ---
    def upsert_object(self, obj: ObjectRecord) -> ObjectRecord:
        return self._store.upsert_object(
            obj.model_copy(update={"properties": _bounded(obj.properties)})
        )

    def upsert_relation(self, relation: RelationRecord) -> RelationRecord:
        rid = relation.id or relation_id(relation.source, relation.relation, relation.target)
        return self._store.upsert_relation(
            relation.model_copy(
                update={"id": rid, "properties": _bounded(relation.properties)}
            )
        )

    # -------------------------------------------------------------- reads ---
    def get_object(self, object_id: str) -> ObjectRecord | None:
        for obj in self._store.objects():
            if obj.id == object_id:
                return obj
        return None

    def find_objects(self, *, type: str | None = None) -> list[ObjectRecord]:
        matched = [o for o in self._store.objects() if type is None or o.type == type]
        return sorted(matched, key=lambda o: o.id)

    def get_relation(self, relation_id: str) -> RelationRecord | None:
        for rel in self._store.relations():
            if rel.id == relation_id:
                return rel
        return None

    def relations(self) -> list[RelationRecord]:
        return sorted(self._store.relations(), key=_rel_order)

    def neighbors(self, object_id: str, relation: str | None = None) -> list[RelationRecord]:
        """Depth-1 relations touching ``object_id``, explicit order."""
        hits = self._store.neighbors(object_id, relation=relation)
        return sorted(hits, key=_rel_order)

    def adjacent(self, object_id: str, relation: str | None = None) -> list[ObjectRecord]:
        """Depth-1 neighbor OBJECTS (the other end of each relation)."""
        other: dict[str, ObjectRecord] = {}
        for rel in self.neighbors(object_id, relation):
            other_id = rel.target if rel.source == object_id else rel.source
            if other_id == object_id:
                continue  # self-relation: no other end
            obj = self.get_object(other_id)
            if obj is not None:
                other[obj.id] = obj
        return [other[k] for k in sorted(other)]


def _rel_order(rel: RelationRecord) -> tuple[str, str]:
    """Explicit stable ordering: relation type, then relation id."""
    return (rel.relation, rel.id)
