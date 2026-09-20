"""Memory storage boundary (SPEC §32, §52E-G; Phase 11A).

DATA-ONLY boundary, enforced by construction:

- the public surface exposes exactly five data operations (write, query,
  upsert_object, upsert_relation, neighbors) plus read views — there is no
  method that can grant, authorize, revoke, consume, execute, answer an
  owner conflict, or mark anything SUCCESS;
- nothing here imports authority/executor/verification code, so no call
  chain can exist from memory into the authority path;
- authority-shaped strings (relation names like ``allowed``) are inert
  DATA: they are stored and retrievable, and no code anywhere consults
  memory when deciding authorization (SPEC §32, §52E).

Storage model: one JSONL file, one JSON object per line, envelope
``{"storage_type": "record|object|relation", "data": {...}}``. Every
mutation rewrites the file atomically (tempfile + os.replace, AuthorityStore
pattern) after enforcing the per-collection cap (oldest evicted first), so
storage is bounded and deterministic. On load, malformed lines are dropped
and counted; an unreadable file behaves as an EMPTY store — corruption never
propagates as an exception, and the next write atomically replaces the
whole file with valid content (self-healing compaction).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    ObjectRecord,
    RelationRecord,
    RetrievedMemory,
)
from nomadicos.kernel.config import MemoryConfig

_RECORD = "record"
_OBJECT = "object"
_RELATION = "relation"

_TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2]


def _matches(record: MemoryRecord, tokens: list[str]) -> bool:
    tags = " ".join(record.tags).lower()
    return any(t in record.content.lower() or t in tags for t in tokens)


class MemoryStore:
    """Typed data boundary for memory (SPEC §32). Implementations must stay
    free of any authority-shaped operation."""

    def write(self, record: MemoryRecord) -> MemoryRecord:  # pragma: no cover - protocol
        raise NotImplementedError

    def query(self, query: MemoryQuery) -> list[RetrievedMemory]:  # pragma: no cover - protocol
        raise NotImplementedError

    def upsert_object(self, obj: ObjectRecord) -> ObjectRecord:  # pragma: no cover - protocol
        raise NotImplementedError

    def upsert_relation(  # pragma: no cover - protocol
        self, relation: RelationRecord
    ) -> RelationRecord:
        raise NotImplementedError

    def neighbors(  # pragma: no cover - protocol
        self, object_id: str, relation: str | None = None
    ) -> list[RelationRecord]:
        raise NotImplementedError

    def objects(self) -> list[ObjectRecord]:  # pragma: no cover - protocol
        raise NotImplementedError

    def relations(self) -> list[RelationRecord]:  # pragma: no cover - protocol
        raise NotImplementedError

    def records(  # pragma: no cover - protocol
        self, kind: MemoryKind | None = None
    ) -> list[MemoryRecord]:
        raise NotImplementedError


class JsonlMemoryStore(MemoryStore):
    """Bounded JSONL memory store; one atomically-replaced file.

    Corrupt lines are dropped on load (never raised); the store then reads
    as the remaining valid entries, and the next write atomically compacts
    the file back to fully valid content.
    """

    def __init__(self, path: str | Path, config: MemoryConfig | None = None) -> None:
        self.path = Path(path)
        self._max = (config or MemoryConfig()).max_records_per_kind
        self._records: dict[str, MemoryRecord] = {}
        self._objects: dict[str, ObjectRecord] = {}
        self._relations: dict[str, RelationRecord] = {}
        self.dropped_invalid_lines = 0
        self._load()

    # -------------------------------------------------------------- read ---
    def records(self, kind: MemoryKind | None = None) -> list[MemoryRecord]:
        return [r for r in self._records.values() if kind is None or r.kind is kind]

    def objects(self) -> list[ObjectRecord]:
        return list(self._objects.values())

    def relations(self) -> list[RelationRecord]:
        return list(self._relations.values())

    def query(self, query: MemoryQuery) -> list[RetrievedMemory]:
        """Deterministic bounded filter over stored records.

        11A semantics: kind filter + optional keyword match on content/tags,
        file order, last ``limit`` entries kept, uniform score 1.0 (this is
        a bounded fetch, not semantic ranking — ranking lives in 11F).
        """
        kinds = set(query.kinds) or set(MemoryKind)
        tokens = _tokens(query.text)
        matched = [
            RetrievedMemory(record=r, score=1.0)
            for r in self._records.values()
            if r.kind in kinds and (not tokens or _matches(r, tokens))
        ]
        return matched[-query.limit :]

    def neighbors(self, object_id: str, relation: str | None = None) -> list[RelationRecord]:
        return [
            rel
            for rel in self._relations.values()
            if (relation is None or rel.relation == relation)
            and object_id in (rel.source, rel.target)
        ]

    # -------------------------------------------------------------- write ---
    def write(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.id] = record
        self._evict_records()
        self._persist()
        return record

    def upsert_object(self, obj: ObjectRecord) -> ObjectRecord:
        self._objects[obj.id] = obj
        while len(self._objects) > self._max:
            del self._objects[next(iter(self._objects))]
        self._persist()
        return obj

    def upsert_relation(self, relation: RelationRecord) -> RelationRecord:
        self._relations[relation.id] = relation
        while len(self._relations) > self._max:
            oldest = next(iter(self._relations))
            del self._relations[oldest]
        self._persist()
        return relation

    # ----------------------------------------------------------- internal ---
    def _evict_records(self) -> None:
        """Keep at most ``max`` records per kind; drop the oldest first."""
        per_kind: dict[MemoryKind, list[str]] = {}
        for rec in self._records.values():
            per = per_kind.setdefault(rec.kind, [])
            per.append(rec.id)
        for ids in per_kind.values():
            for rec_id in ids[: max(len(ids) - self._max, 0)]:
                del self._records[rec_id]

    def _persist(self) -> None:
        """Rewrite the whole file atomically: valid entries only (the corrupt
        lines dropped at load time never come back)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for tag, items in (
                    (_RECORD, self._records.values()),
                    (_OBJECT, self._objects.values()),
                    (_RELATION, self._relations.values()),
                ):
                    for item in items:
                        line = {"storage_type": tag, "data": item.model_dump(mode="json")}
                        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):  # pragma: no cover - only on failure
                os.unlink(tmp)

    def _load(self) -> None:
        """Fail-safe load: malformed lines are dropped (counted); an
        unreadable file reads as EMPTY. Never raises."""
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        self._records = {}
        self._objects = {}
        self._relations = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                tag = raw["storage_type"]
                data = raw["data"]
            except (json.JSONDecodeError, KeyError, TypeError):
                self.dropped_invalid_lines += 1
                continue
            try:
                if tag == _RECORD:
                    rec = MemoryRecord.model_validate(data)
                    self._records[rec.id] = rec
                elif tag == _OBJECT:
                    obj = ObjectRecord.model_validate(data)
                    self._objects[obj.id] = obj
                elif tag == _RELATION:
                    rel = RelationRecord.model_validate(data)
                    self._relations[rel.id] = rel
                else:
                    self.dropped_invalid_lines += 1
            except (ValidationError, ValueError):
                self.dropped_invalid_lines += 1
