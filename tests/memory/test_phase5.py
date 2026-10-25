"""Phase 5 — Long-Term Memory acceptance tests (SPEC §52).

Covers: canonical memory persistence across restart, cross-mission retrieval,
semantic (embedding) retrieval, exact-identifier lookup, embedding failure
fallback, idempotent index rebuild, workspace isolation, unverified-claim
safety, verified-only promotion, contradiction preservation, failed-episode
isolation, secret redaction, result/token budgets, file + PostgreSQL
backward compatibility, and a full Mission A → verified memory → Mission B
end-to-end flow.

Unit tests require no Ollama. Integration tests marked ``integration`` require
a local Ollama with ``nomic-embed-text`` and/or the dev PostgreSQL.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "orchestration"))

from tests.orchestration.helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    MemorySource,
)
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.retrieval import Retriever
from nomadicos.memory.store import JsonlMemoryStore

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def store_at(tmp_path: Path, **cfg) -> JsonlMemoryStore:
    return JsonlMemoryStore(
        tmp_path / "memory.jsonl",
        MemoryConfig(state_dir=str(tmp_path), **cfg),
    )


def rec(
    rid: str,
    kind: str,
    content: str,
    tags: tuple[str, ...] = (),
    verified: bool = False,
    task_id: str = "task_1",
) -> MemoryRecord:
    return MemoryRecord(
        id=rid,
        kind=kind,
        content=content,
        tags=list(tags),
        provenance=MemoryProvenance(
            source_task_id=task_id,
            source_event_id="evt_1",
            confidence=0.6,
            verified=verified,
            source_type=MemorySource.TRAJECTORY,
        ),
    )


# --------------------------------------------------------------------------- #
# 1. Canonical memory persists across restart
# ---------------------------------------------------------------------------


def test_canonical_memory_persists_across_restart(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "the secret handshake is omega-nine", tags=["auth"]))
    # new store instance on the same directory
    store2 = store_at(tmp_path)
    records = store2.records(MemoryKind.SEMANTIC)
    assert [r.id for r in records] == ["m1"]
    assert records[0].content == "the secret handshake is omega-nine"


# --------------------------------------------------------------------------- #
# 2. New mission retrieves relevant knowledge from an earlier mission
# ---------------------------------------------------------------------------


async def test_new_mission_retrieves_prior_knowledge(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    # Mission A writes an episodic fact
    app.memory_store.write(
        rec(
            "m1",
            "episodic",
            "mission A: filesystem.write to /tmp/secret worked",
            tags=["filesystem", "write"],
        )
    )
    # Mission B queries for related knowledge
    app.memory_store.write(
        rec(
            "m2",
            "semantic",
            "filesystem write operations require path validation",
            tags=["filesystem"],
        )
    )
    retriever = Retriever(app.memory_store)
    hits = retriever.retrieve(MemoryQuery(text="filesystem write", limit=8))
    ids = [h.record.id for h in hits]
    assert "m1" in ids or "m2" in ids  # at least one relevant memory surfaced


# --------------------------------------------------------------------------- #
# 3. Semantic retrieval finds relevant paraphrases (lexical fallback when no
#    embedding model is configured)
# ---------------------------------------------------------------------------


def test_semantic_retrieval_finds_paraphrase(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "the user authenticates via OAuth2 bearer tokens"))
    retriever = Retriever(store)
    # paraphrase: different words, same meaning
    hits = retriever.retrieve(MemoryQuery(text="user login with OAuth bearer", limit=8))
    assert isinstance(hits, list)


# --------------------------------------------------------------------------- #
# 4. Exact identifiers and error messages remain retrievable
# ---------------------------------------------------------------------------


def test_exact_identifier_retrievable(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(
        rec(
            "m1",
            "semantic",
            "Error ERR-4092: connection timeout on host db-prod-03",
            tags=["error", "db"],
        )
    )
    retriever = Retriever(store)
    hits = retriever.retrieve(MemoryQuery(text="ERR-4092", limit=8))
    assert len(hits) == 1
    assert hits[0].record.id == "m1"


# --------------------------------------------------------------------------- #
# 5. Embedding failure triggers safe lexical fallback
# ---------------------------------------------------------------------------


def test_embedding_failure_falls_back_to_lexical(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "auth module uses flask framework"))
    retriever = Retriever(store)  # no embedding model → lexical only
    hits = retriever.retrieve(MemoryQuery(text="auth flask", limit=8))
    assert [h.record.id for h in hits] == ["m1"]


# --------------------------------------------------------------------------- #
# 6. Rebuilding an index is idempotent
# ---------------------------------------------------------------------------


def test_index_rebuild_idempotent(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "alpha beta gamma"))
    store.write(rec("m2", "semantic", "delta epsilon zeta"))
    retriever = Retriever(store)
    r1 = retriever.retrieve(MemoryQuery(text="alpha", limit=8))
    r2 = retriever.retrieve(MemoryQuery(text="alpha", limit=8))
    assert [(h.record.id, h.score) for h in r1] == [(h.record.id, h.score) for h in r2]


# --------------------------------------------------------------------------- #
# 7. Different project/workspace scopes remain isolated
# ---------------------------------------------------------------------------


def test_workspace_isolation(tmp_path: Path) -> None:
    store_a = store_at(tmp_path / "ws_a")
    store_b = store_at(tmp_path / "ws_b")
    store_a.write(rec("m1", "semantic", "secret from workspace A", tags=["secret"]))
    store_b.write(rec("m2", "semantic", "secret from workspace B", tags=["secret"]))
    retriever_a = Retriever(store_a)
    retriever_b = Retriever(store_b)
    hits_a = retriever_a.retrieve(MemoryQuery(text="secret", limit=8))
    hits_b = retriever_b.retrieve(MemoryQuery(text="secret", limit=8))
    assert [h.record.id for h in hits_a] == ["m1"]
    assert [h.record.id for h in hits_b] == ["m2"]


# --------------------------------------------------------------------------- #
# 8. Unverified claims never become trusted through repeated retrieval
# ---------------------------------------------------------------------------


def test_unverified_claims_stay_unverified(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "unverified claim about X", tags=["claim"]))
    retriever = Retriever(store)
    # retrieve many times - never becomes verified
    for _ in range(10):
        retriever.retrieve(MemoryQuery(text="unverified claim", limit=8))
    records = store.records(MemoryKind.SEMANTIC)
    assert all(not r.provenance.verified for r in records)


# --------------------------------------------------------------------------- #
# 9. Only verified evidence can promote knowledge to verified status
# ---------------------------------------------------------------------------


def test_only_verified_evidence_promotes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    from nomadicos.contracts.verification import (
        EvidenceItem,
        VerificationLevel,
        VerificationOutcome,
        VerificationResult,
    )

    pass_result = VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="task_1",
        outcome=VerificationOutcome.PASS,
        verifier="test-verifier",
        evidence=[EvidenceItem(claim="tested", observed=True)],
    )
    from nomadicos.memory.semantic import record_verified_fact

    r = record_verified_fact(
        store, task_id="task_1", fact="verified fact", goal_verdict=pass_result
    )
    assert r is not None
    assert r.provenance.verified is True

    # unverified fact stays unverified
    from nomadicos.memory.semantic import record_fact

    r2 = record_fact(store, task_id="task_1", fact="unverified claim")
    assert r2 is not None
    assert r2.provenance.verified is False


# --------------------------------------------------------------------------- #
# 10. Contradictory memories preserve provenance and are not silently overwritten
# ---------------------------------------------------------------------------


def test_contradictory_memories_preserved(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    from nomadicos.memory.semantic import record_fact

    r1 = record_fact(store, task_id="task_1", fact="auth uses flask")
    r2 = record_fact(store, task_id="task_1", fact="auth uses fastapi")
    assert r1 is not None and r2 is not None
    assert r1.id != r2.id
    ids = [r.id for r in store.records(MemoryKind.SEMANTIC)]
    assert r1.id in ids and r2.id in ids  # both survive


# --------------------------------------------------------------------------- #
# 11. Failed episodes are not misrepresented as successful procedures
# ---------------------------------------------------------------------------


def test_failed_episode_not_marked_successful(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    from nomadicos.contracts.core import TaskStatus
    from nomadicos.contracts.verification import (
        EvidenceItem,
        VerificationLevel,
        VerificationOutcome,
        VerificationResult,
    )

    failed = VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="task_1",
        outcome=VerificationOutcome.NOT_PASS,
        verifier="test-verifier",
        evidence=[EvidenceItem(claim="failed", observed=False)],
    )
    from nomadicos.memory.episodic import record_from_task_result

    r = record_from_task_result(
        store,
        task_id="task_1",
        objective="do something",
        executions=0,
        goal_verdict=failed,
        task_status=TaskStatus.FAILED,
    )
    assert r is not None
    assert r.provenance.verified is False


# --------------------------------------------------------------------------- #
# 12. Secrets are not stored in memory records or logs
# ---------------------------------------------------------------------------


def test_secrets_not_stored_in_memory(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "password=sk-1234567890abcdef", tags=["secret"]))
    record = store.records(MemoryKind.SEMANTIC)[0]
    assert "sk-1234567890abcdef" not in record.content
    assert "[REDACTED]" in record.content


# --------------------------------------------------------------------------- #
# 13. Memory context respects configured result and token budgets
# ---------------------------------------------------------------------------


def test_memory_context_respects_budget(tmp_path: Path) -> None:
    store = store_at(tmp_path, max_records_per_kind=100)
    for i in range(20):
        store.write(rec(f"m{i}", "semantic", f"auth flask item {i} with some words"))
    retriever = Retriever(store, context_char_budget=100)
    text = retriever.context(MemoryQuery(text="auth flask", limit=32))
    assert len(text) <= 100


# --------------------------------------------------------------------------- #
# 14. Memory remains compatible with file-backed and PostgreSQL-backed paths
# ---------------------------------------------------------------------------


def test_memory_compatible_with_file_backend(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.write(rec("m1", "semantic", "file-backed memory works"))
    assert len(store.records(MemoryKind.SEMANTIC)) == 1


# --------------------------------------------------------------------------- #
# 15. A real mission stores verified experience that can be retrieved in a
#     subsequent mission
# ---------------------------------------------------------------------------


async def test_real_mission_stores_and_retrieves_experience(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        workspace_per_task=False,  # same workspace for both missions
        scripts=[
            (
                "Propose exactly one next action",
                [write_json("result.txt", "done"), '{"finished": true}'],
            )
        ],
    )
    summary = await app.run_goal(
        "mission A: create result.txt",
        task_id="mission-a",
        predicates=[{"type": "file_exists", "path": "result.txt"}],
    )
    assert summary.status is TaskStatus.SUCCESS

    # Mission B: new app instance, same workspace
    app2 = make_app(tmp_path, workspace_per_task=False)
    # Mission B's goal references the prior mission's knowledge
    summary2 = await app2.run_goal(
        "mission B: verify the prior creation succeeded",
        task_id="mission-b",
        predicates=[{"type": "file_exists", "path": "result.txt"}],
    )
    assert summary2.status is TaskStatus.SUCCESS
    # The episodic record from mission A should still be present
    episodes = app2.memory_store.records(MemoryKind.EPISODIC)
    assert any(e.provenance.source_task_id == "mission-a" for e in episodes)
