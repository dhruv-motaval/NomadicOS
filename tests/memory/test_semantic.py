"""Phase 11D — semantic memory tests.

Trusted facts come only from real verifier evidence; contradictions are
preserved append-only; identity is deterministic and content-addressed;
no authority/executor/SUCCESS paths exist.
"""

from __future__ import annotations

import inspect

from nomadicos.contracts.memory import MemoryKind
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.semantic import (
    TAG_UNVERIFIED_FACT,
    TAG_VERIFIED_FACT,
    fact_id,
    record_fact,
    record_verified_fact,
)
from nomadicos.memory.store import JsonlMemoryStore

REAL_VERIFIER = "nomadic-goal-verifier-v1"


def pass_result() -> VerificationResult:
    return VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="task_1",
        outcome=VerificationOutcome.PASS,
        verifier=REAL_VERIFIER,
        evidence=[EvidenceItem(claim="tests pass", observed=True)],
    )


def not_pass_result() -> VerificationResult:
    return VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="task_1",
        outcome=VerificationOutcome.NOT_PASS,
        verifier=REAL_VERIFIER,
        evidence=[EvidenceItem(claim="predicate failed", observed=False)],
    )


def store_at(tmp_path) -> JsonlMemoryStore:
    return JsonlMemoryStore(
        tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path))
    )


def test_verified_fact_stored_and_round_trips(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_verified_fact(
        store,
        task_id="task_1",
        fact="project auth uses flask",
        goal_verdict=pass_result(),
        source_event_id="evt_7",
    )
    assert got is not None and got.kind is MemoryKind.SEMANTIC
    assert got.provenance.verified is True
    assert got.provenance.confidence == 0.95
    assert TAG_VERIFIED_FACT in got.tags
    reloaded = store_at(tmp_path).records(MemoryKind.SEMANTIC)
    assert [r.id for r in reloaded] == [got.id]
    assert "project auth uses flask" in reloaded[0].content


def test_unverified_fact_stored_explicitly_unverified(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_fact(store, task_id="task_1", fact="auth uses flask")
    assert got is not None
    assert got.provenance.verified is False
    assert got.provenance.confidence <= 0.95
    assert TAG_UNVERIFIED_FACT in got.tags
    assert TAG_VERIFIED_FACT not in got.tags


def test_claims_cannot_become_verified_fact(tmp_path) -> None:
    store = store_at(tmp_path)
    assert (
        record_verified_fact(
            store, task_id="task_1", fact="model said it works", goal_verdict=None  # type: ignore[arg-type]
        )
        is None
    )
    legacy = pass_result().model_copy(update={"verifier": "legacy-placeholder"})
    assert record_verified_fact(store, task_id="t", fact="f", goal_verdict=legacy) is None
    assert (
        record_verified_fact(
            store, task_id="task_1", fact="x", goal_verdict=not_pass_result()
        )
        is None
    )
    assert store.records(MemoryKind.SEMANTIC) == []


def test_deterministic_fact_identity(tmp_path) -> None:
    store = store_at(tmp_path)
    a = record_fact(store, task_id="task_1", fact="auth uses flask")
    b = record_fact(store, task_id="task_1", fact="auth  uses   flask")
    assert a is not None and b is not None
    assert a.id == b.id == fact_id("auth uses FLASK ", "task_1")  # normalized
    other = record_fact(store, task_id="task_1", fact="auth uses fastapi")
    assert other is not None and other.id != a.id
    c = record_fact(store, task_id="task_2", fact="auth uses flask")
    assert c is not None and c.id != a.id  # distinct observation, preserved


def test_exact_duplicate_is_deterministic(tmp_path) -> None:
    store = store_at(tmp_path)
    first = record_fact(store, task_id="task_1", fact="same fact")
    second = record_fact(store, task_id="task_1", fact="same fact")
    assert first is not None and first.id == second.id
    assert len(store.records(MemoryKind.SEMANTIC)) == 1


def test_contradictions_preserved_append_only(tmp_path) -> None:
    store = store_at(tmp_path)
    older = record_fact(store, task_id="task_1", fact="auth uses flask")
    newer = record_fact(store, task_id="task_1", fact="auth uses fastapi")
    assert older is not None and newer is not None and older.id != newer.id
    ids = [r.id for r in store.records(MemoryKind.SEMANTIC)]
    assert older.id in ids and newer.id in ids  # both survive: no latest-wins
    again = record_fact(store, task_id="task_1", fact="auth uses flask")
    assert again is not None and again.id == older.id
    ids2 = [r.id for r in store.records(MemoryKind.SEMANTIC)]
    assert sorted(ids2) == sorted({older.id, newer.id})  # deterministic replace


def test_unverified_to_verified_upgrade_is_deterministic(tmp_path) -> None:
    store = store_at(tmp_path)
    unverified = record_fact(store, task_id="task_1", fact="auth uses flask")
    verified = record_verified_fact(
        store, task_id="task_1", fact="auth uses flask", goal_verdict=pass_result()
    )
    assert unverified is not None and verified is not None
    assert verified.id == unverified.id  # same identity: evidence upgrade
    assert verified.provenance.verified is True
    assert len(store.records(MemoryKind.SEMANTIC)) == 1


def test_provenance_preserved(tmp_path) -> None:
    store = store_at(tmp_path)
    record_verified_fact(
        store,
        task_id="task_1",
        fact="db is postgres",
        goal_verdict=pass_result(),
        source_event_id="evt_42",
    )
    got = store_at(tmp_path).records(MemoryKind.SEMANTIC)[0]
    assert got.provenance.source_task_id == "task_1"
    assert got.provenance.source_event_id == "evt_42"
    assert got.provenance.confidence == 0.95
    assert got.provenance.verified is True


def test_content_bounds(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_fact(store, task_id="task_1", fact="word " * 2000)
    assert got is not None
    assert len(got.content) <= 400
    assert len(got.tags) <= 3
    assert record_fact(store, task_id="task_1", fact="   ") is None


def test_no_authority_imports_and_no_success_path() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.semantic", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.orchestration",
        "nomadicos.router",
        "import langgraph",
        "AuthorizedAction",
        "TaskStatus",
        "def write(",
        "subprocess",
    ):
        assert forbidden not in source
