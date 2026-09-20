"""Memory brick — context DATA only (SPEC §32, §52E-G; Phase 11A).

Memory is a replaceable context brick behind typed contracts. It is never
authority: it cannot grant permissions, authorize or revoke actions, answer
owner conflicts, execute tools, or record task SUCCESS. Every stored item
carries provenance; content stored from any source (model text included)
remains DATA that prompt builders must mark as such (§52F wiring in 11F/11G).

Storage contract (11A): append/upsert/query through a fail-safe store.
Corrupt storage degrades to an empty readable state — a memory failure can
never break authorization, execution, or verification guarantees.
"""

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    ObjectRecord,
    RelationRecord,
    RetrievedMemory,
)
from nomadicos.memory.store import JsonlMemoryStore, MemoryStore

__all__ = [
    "JsonlMemoryStore",
    "MemoryKind",
    "MemoryProvenance",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryStore",
    "ObjectRecord",
    "RelationRecord",
    "RetrievedMemory",
]
