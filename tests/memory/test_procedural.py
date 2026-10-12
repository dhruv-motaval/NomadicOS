"""Phase 11D — procedural memory tests.

Trusted procedures come only from real verifier evidence; procedures are
descriptive retrieval data with no execution path; identity is
deterministic; no authority/executor/SUCCESS paths exist.
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
from nomadicos.memory.procedural import (
    TAG_UNVERIFIED_PROCEDURE,
    TAG_VERIFIED_PROCEDURE,
    procedure_id,
    record_procedure,
    record_verified_procedure,
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


def store_at(tmp_path) -> JsonlMemoryStore:
    return JsonlMemoryStore(
        tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path))
    )


STEPS = ("run pytest -q", "repair multiply", "rerun pytest -q")


def test_verified_procedure_stored_and_round_trips(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_verified_procedure(
        store,
        task_id="task_1",
        objective="fix the calculator bug",
        steps=STEPS,
        goal_verdict=pass_result(),
        source_event_id="evt_5",
    )
    assert got is not None and got.kind is MemoryKind.PROCEDURAL
    assert got.provenance.verified is True
    assert got.provenance.confidence == 0.95
    assert TAG_VERIFIED_PROCEDURE in got.tags
    assert "verif:" in got.content
    reloaded = store_at(tmp_path).records(MemoryKind.PROCEDURAL)
    assert [r.id for r in reloaded] == [got.id]
    assert "repair multiply" in reloaded[0].content


def test_unverified_procedure_cannot_become_verified(tmp_path) -> None:
    store = store_at(tmp_path)
    unverified = record_procedure(
        store, task_id="task_1", objective="guess", steps=["step one", "step 2"]
    )
    assert unverified is not None
    assert unverified.provenance.verified is False
    assert TAG_UNVERIFIED_PROCEDURE in unverified.tags
    assert TAG_VERIFIED_PROCEDURE not in unverified.tags
    # without a real verifier artifact nothing becomes a verified procedure
    assert (
        record_verified_procedure(
            store, task_id="task_1", objective="x", steps=["s"], goal_verdict=None  # type: ignore[arg-type]
        )
        is None
    )
    legacy = pass_result().model_copy(update={"verifier": "legacy-placeholder"})
    assert (
        record_verified_procedure(
            store, task_id="t", objective="y", steps=["s"], goal_verdict=legacy
        )
        is None
    )
    assert len(store.records(MemoryKind.PROCEDURAL)) == 1  # preserved, unverified


def test_deterministic_procedure_identity(tmp_path) -> None:
    store = store_at(tmp_path)
    a = record_procedure(store, task_id="task_1", objective="fix calc", steps=STEPS)
    b = record_procedure(
        store,
        task_id="task_1",
        objective="FIX   calc",
        steps=["run  pytest -q", "repair multiply", "rerun pytest -q"],
    )
    assert a is not None and b is not None
    assert a.id == b.id == procedure_id("task_1", "fix calc", STEPS)  # normalized
    different = record_procedure(
        store, task_id="task_1", objective="fix the calculator bug", steps=["other step"]
    )
    assert a.id != (different.id if different else "")


def test_duplicate_procedure_is_deterministic(tmp_path) -> None:
    store = store_at(tmp_path)
    first = record_procedure(store, task_id="task_1", objective="fix  calc", steps=STEPS)
    second = record_procedure(
        store, task_id="task_1", objective="fix calc", steps=STEPS
    )
    assert first is not None and first.id == second.id
    assert len(store.records(MemoryKind.PROCEDURAL)) == 1


def test_distinct_procedures_are_preserved(tmp_path) -> None:
    store = store_at(tmp_path)
    a = record_verified_procedure(
        store, task_id="task_1", objective="fix calc", steps=STEPS,
        goal_verdict=pass_result(),
    )
    b = record_verified_procedure(
        store, task_id="task_2", objective="fix calc", steps=STEPS,
        goal_verdict=pass_result(),
    )
    assert a is not None and b is not None and a.id != b.id  # distinct tasks
    assert len(store.records(MemoryKind.PROCEDURAL)) == 2  # both preserved


def test_provenance_preserved(tmp_path) -> None:
    store = store_at(tmp_path)
    record_verified_procedure(
        store,
        task_id="task_1",
        objective="fix calc",
        steps=STEPS,
        goal_verdict=pass_result(),
        source_event_id="evt_8",
    )
    got = store_at(tmp_path).records(MemoryKind.PROCEDURAL)[0]
    assert got.provenance.source_task_id == "task_1"
    assert got.provenance.source_event_id == "evt_8"
    assert got.provenance.verified is True


def test_procedure_is_retrieval_data_only(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_verified_procedure(
        store, task_id="task_1", objective="fix it", steps=STEPS,
        goal_verdict=pass_result(),
    )
    assert got is not None
    # descriptive text only: no executable payload, no authority fields
    assert got.data_only is True
    assert set(got.tags) <= {"procedure", TAG_VERIFIED_PROCEDURE, "task:task_1"}


def test_content_bounds(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record_verified_procedure(
        store,
        task_id="task_1",
        objective="o" * 5000,
        steps=[f"step-{i} " + "s" * 200 for i in range(12)],
        goal_verdict=pass_result(),
    )
    assert got is not None
    assert len(got.content) <= 600
    assert len(got.tags) == 3
    assert record_procedure(store, task_id="task_1", objective="x", steps=["", "  "]) is None


def test_persistence_round_trip(tmp_path) -> None:
    store = store_at(tmp_path)
    record_procedure(store, task_id="task_1", objective="fix", steps=["a", "b"])
    record_verified_procedure(
        store, task_id="task_2", objective="fix2", steps=["c"], goal_verdict=pass_result()
    )
    reloaded = store_at(tmp_path).records(MemoryKind.PROCEDURAL)
    assert len(reloaded) == 2
    assert {r.provenance.verified for r in reloaded} == {True, False}


def test_no_authority_imports_and_no_success_path() -> None:
    source = inspect.getsource(
        __import__("nomadicos.memory.procedural", fromlist=["x"])
    )
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
        "Popen",
        "os.system",
        "interrupt(",
    ):
        assert forbidden not in source
