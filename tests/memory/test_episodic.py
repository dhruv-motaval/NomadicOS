"""Phase 11C — episodic memory tests.

Verified outcomes come only from real verifier evidence; claims, critic
ACCEPT, and bare executor success can never produce a verified-success
episode; dedup is deterministic; persistence failures fail safe.
"""

from __future__ import annotations

import inspect

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.memory import MemoryKind
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.kernel.config import MemoryConfig
from nomadicos.memory.episodic import (
    TAG_FAILED,
    TAG_PARTIAL,
    TAG_VERIFIED_SUCCESS,
)
from nomadicos.memory.episodic import (
    record_from_task_result as record_episode,
)
from nomadicos.memory.store import JsonlMemoryStore

REAL_VERIFIER = "nomadic-goal-verifier-v1"


def goal_result(outcome: VerificationOutcome) -> VerificationResult:
    if outcome is VerificationOutcome.PASS:
        evidence = [EvidenceItem(claim="predicate tests_pass", observed=True)]
    elif outcome is VerificationOutcome.NOT_VERIFIED:
        evidence = [
            EvidenceItem(claim="nothing verifiable", observed=False, unverifiable=True)
        ]
    else:
        evidence = [EvidenceItem(claim="predicate failed", observed=False)]
    return VerificationResult(
        level=VerificationLevel.GOAL,
        task_id="task_1",
        outcome=outcome,
        verifier=REAL_VERIFIER,
        evidence=evidence,
    )


def store_at(tmp_path) -> JsonlMemoryStore:
    return JsonlMemoryStore(
        tmp_path / "memory.jsonl", MemoryConfig(state_dir=str(tmp_path))
    )


def record(store, **kw):
    kw.setdefault("objective", "fix the login bug")
    return record_episode(store, task_id="task_1", **kw)


def test_verified_success_creates_episodic_record(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record(
        store,
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=4,
        test_exits=(1, 0),
        source_event_id="evt_9",
    )
    assert got is not None and got.kind is MemoryKind.EPISODIC
    assert got.provenance.verified is True
    assert got.provenance.confidence == 0.95
    assert TAG_VERIFIED_SUCCESS in got.tags
    assert "verif:" in got.content
    assert "test_exits=[1, 0]" in got.content
    reloaded = store_at(tmp_path).records(MemoryKind.EPISODIC)
    assert [r.id for r in reloaded] == [got.id]  # durable via 11A store
    assert reloaded[0].provenance.source_task_id == "task_1"


def test_unverified_outcome_is_never_a_verified_success(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record(
        store,
        objective="fix it",
        goal_verdict=goal_result(VerificationOutcome.NOT_VERIFIED),
        task_status=TaskStatus.PARTIAL,
        executions=2,
    )
    assert got is not None
    assert got.provenance.verified is False
    assert TAG_PARTIAL in got.tags
    assert TAG_VERIFIED_SUCCESS not in got.tags


def test_model_claim_alone_records_nothing(tmp_path) -> None:
    store = store_at(tmp_path)
    # the model said SUCCESS (status relayed) but no verifier artifact exists
    assert record(store, objective="trust me", task_status=TaskStatus.SUCCESS) is None
    assert store.records(MemoryKind.EPISODIC) == []


def test_critic_accept_alone_records_nothing(tmp_path) -> None:
    store = store_at(tmp_path)
    # critic ACCEPT carries no verifier evidence -> no episode
    assert record(store, objective="critic liked it") is None
    assert store.records(MemoryKind.EPISODIC) == []


def test_executor_success_without_verification_is_partial(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record(
        store,
        objective="ran tests",
        executions=2,
        test_exits=(0, 0),
        task_status=TaskStatus.PARTIAL,
    )
    assert got is not None
    assert got.provenance.verified is False
    assert TAG_PARTIAL in got.tags
    assert TAG_VERIFIED_SUCCESS not in got.tags
    assert "executions=2" in got.content


def test_provenance_preserved(tmp_path) -> None:
    store = store_at(tmp_path)
    record(
        store,
        objective="fix it",
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=2,
        test_exits=(0,),
        source_event_id="evt_42",
    )
    got = store_at(tmp_path).records(MemoryKind.EPISODIC)[0]
    assert got.provenance.source_task_id == "task_1"
    assert got.provenance.source_event_id == "evt_42"
    assert got.provenance.confidence == 0.95
    assert got.provenance.verified is True


def test_failed_episode_preserved_not_success(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record(
        store,
        objective="will not pass",
        goal_verdict=goal_result(VerificationOutcome.NOT_PASS),
        task_status=TaskStatus.FAILED,
        executions=2,
    )
    assert got is not None
    assert TAG_FAILED in got.tags
    assert got.provenance.verified is False
    assert TAG_VERIFIED_SUCCESS not in got.tags


def test_dedup_is_deterministic_and_idempotent(tmp_path) -> None:
    store = store_at(tmp_path)
    kwargs = dict(
        objective="fix the bug",
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=2,
        test_exits=(0,),
    )
    first = record(store, **kwargs)
    second = record(store, **kwargs)
    assert first is not None and first.id == second.id
    assert len(store.records(MemoryKind.EPISODIC)) == 1  # id-keyed replace
    # a failure episode for the same task is a DIFFERENT, preserved episode
    failed = record(
        store,
        objective="fix the bug",
        goal_verdict=goal_result(VerificationOutcome.NOT_PASS),
        task_status=TaskStatus.FAILED,
        executions=2,
    )
    assert failed is not None and failed.id != first.id
    assert len(store.records(MemoryKind.EPISODIC)) == 2  # both episodes kept


def test_bounded_content_and_refs(tmp_path) -> None:
    store = store_at(tmp_path)
    got = record(
        store,
        objective="x" * 5000,
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=1,
        evidence_refs=[f"e{i}" for i in range(12)],
    )
    assert got is not None
    assert len(got.content) <= 600
    assert len(got.tags) <= 4


def test_repeated_identical_recording_is_deterministic(tmp_path) -> None:
    store = store_at(tmp_path)
    kwargs = dict(
        objective="same goal",
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=2,
        test_exits=(0,),
    )
    a = record(store, **kwargs)
    b = record(store, **kwargs)
    assert a is not None and b is not None
    assert a.id == b.id and a.content == b.content and a.tags == b.tags
    assert a.provenance.verified is b.provenance.verified is True
    assert len(store.records(MemoryKind.EPISODIC)) == 1


def test_write_failure_is_fail_safe() -> None:
    class _BrokenStore:
        def write(self, record):  # pragma: no cover - simulated disk failure
            raise OSError("disk full")

    got = record(
        _BrokenStore(),
        objective="fine task",
        goal_verdict=goal_result(VerificationOutcome.PASS),
        task_status=TaskStatus.SUCCESS,
        executions=2,
    )
    assert got is None  # no exception escapes into the task path


# ------------------------------------------------------ isolation checks ---


def test_no_authority_executor_or_routing_imports() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.episodic", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.orchestration",
        "nomadicos.router",
        "import langgraph",
        "AuthorizedAction",
        "interrupt(",
    ):
        assert forbidden not in source


def test_no_success_write_path_exists() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.episodic", fromlist=["x"]))
    assert "TaskStatus.SUCCESS" in source  # only as a READ (classification)
    assert "= TaskStatus.SUCCESS" not in source  # never assigned
    assert "task_status =" not in source  # never writes a task status
    assert "def write(" not in source  # it owns no write path of its own
