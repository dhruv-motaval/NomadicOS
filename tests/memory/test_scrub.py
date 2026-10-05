"""Sprint 5 — secret exclusion at the memory write boundary (§52G).

The JsonlMemoryStore scrubs every durable write: credentials and runtime
secrets are redacted deterministically; ordinary content round-trips.
"""

from __future__ import annotations

from pathlib import Path

from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryRecord,
    ObjectRecord,
    RelationRecord,
)
from nomadicos.memory.scrub import REDACTED, scrub
from nomadicos.memory.store import JsonlMemoryStore


def store_at(tmp_path: Path) -> JsonlMemoryStore:
    return JsonlMemoryStore(tmp_path / "memory.jsonl")


def rec(rid: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        id=rid,
        kind=MemoryKind.SEMANTIC,
        content=content,
        provenance=MemoryProvenance(source_task_id="t", confidence=0.5),
    )


def test_store_never_persists_assignment_secrets(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    stored = store.write(rec("m1", "connect with password=hunter2 and api_key: sk123"))
    assert "hunter2" not in stored.content
    assert "abc123" not in stored.content
    assert REDACTED in stored.content
    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    assert "hunter2" not in next(r for r in reloaded.records() if r.id == "m1").content


def test_store_never_persists_key_material(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    pem = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
    stored = store.write(rec("m2", f"deploy key {pem} used"))
    assert "BEGIN RSA" not in stored.content
    assert REDACTED in stored.content


def test_store_never_persists_well_known_token_shapes(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    stored = store.write(
        rec("m3", "leaked ghp_ABCDEFGHIJKLMNOPQRSTUVWX and AKIAIOSFODNN7EXAMPLE")
    )
    assert "ghp_" not in stored.content
    assert "AKIA" not in stored.content


def test_graph_property_values_are_scrubbed(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    stored = store.upsert_object(
        ObjectRecord(id="svc:api", type="service", properties={"env": "api_key=zzz123"})
    )
    assert REDACTED in stored.properties["env"]
    stored_rel = store.upsert_relation(
        RelationRecord(
            id="rel_x", source="a", relation="uses", target="b",
            properties={"note": "token=abc123"},
        )
    )
    assert REDACTED in stored_rel.properties["note"]


def test_clean_content_roundtrips_verbatim(tmp_path: Path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    content = "auth module uses flask; refs verif_1"
    stored = store.write(rec("m3", content))
    assert stored.content == content
    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    assert reloaded.records()[0].content == stored.content


def test_scrub_is_deterministic() -> None:
    text = "token=abc123 ghp_ABCDEFGHIJKLMNOPQRSTUVWX"
    assert scrub(text) == scrub(text)
    assert "token=" not in scrub(text)


def test_scrub_passes_non_strings_through() -> None:
    assert scrub(2) == 2  # type: ignore[arg-type]
    assert scrub(None) is None  # type: ignore[arg-type]
    assert scrub("") == ""
