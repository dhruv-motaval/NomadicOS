"""Sprint 5 — brain memory controller: the ONE goal-aware retrieval seam
feeding BrainState. Protocol-based (brain never imports the memory brick),
deterministic, bounded, DATA-only."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.brain.memory import (
    MemoryController,
    MemoryRetriever,
    RetrievalPolicy,
    trajectory_steps,
)
from nomadicos.brain.schemas import (
    ActionCandidate,
    CandidateKind,
    MemoryRef,
    Subgoal,
    WorkingMemoryView,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.execution import Observation, ObservationKind
from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryProvenance,
    MemoryQuery,
    MemoryRecord,
    RetrievedMemory,
    TrajectoryStepKind,
)
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.memory.retrieval import Retriever
from nomadicos.memory.semantic import record_fact, record_verified_fact
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.supersession import supersede
from nomadicos.memory.trajectory import record_trajectory


def goal(pred_path: str = "out.txt") -> Goal:
    return Goal(
        id="task_g",
        objective="fix the auth module",
        predicates=[parse_predicate({"type": "file_exists", "path": pred_path})],
    )


def subgoal() -> Subgoal:
    return Subgoal(id="subgoal_s1", goal_id="task_g", description="write failing test")


def state(active: bool = False) -> BrainState:
    from nomadicos.brain.planner.task_graph import TaskGraph

    if active:
        return BrainState(
            goal=goal(),
            task_graph=TaskGraph.build(goal_id="task_g", subgoals=[subgoal()]),
            active_subgoal=subgoal(),
        )
    return BrainState.for_goal(goal())


def store(tmp_path: Path) -> JsonlMemoryStore:
    return JsonlMemoryStore(tmp_path / "memory.jsonl")


def verified_record() -> MemoryRecord:
    return MemoryRecord(
        id="mem_fix",
        kind=MemoryKind.SEMANTIC,
        content="auth module fixed with fastapi tokens",
        provenance=MemoryProvenance(source_task_id="task_prev", confidence=0.95, verified=True),
    )


class FakeRetriever:
    """Deterministic protocol stub (no memory-brick dependency needed)."""

    def __init__(self, hits: list[RetrievedMemory]) -> None:
        self.hits = hits
        self.queries: list[MemoryQuery] = []

    def retrieve(self, query: MemoryQuery) -> list[RetrievedMemory]:
        self.queries.append(query)
        return self.hits


def test_memory_retriever_protocol_is_satisfied_by_real_retriever(tmp_path: Path) -> None:
    retriever = Retriever(store(tmp_path))
    assert isinstance(retriever, MemoryRetriever)  # structural protocol


def test_build_query_uses_goal_and_subgoal_deterministically(tmp_path: Path) -> None:
    controller = MemoryController(FakeRetriever([]))
    active = state(active=True)
    q1 = controller.build_query(active)
    q2 = controller.build_query(active)
    assert q1 == q2  # deterministic
    assert "fix the auth module" in q1.text
    assert "write failing test" in q1.text
    assert q1.min_score == 0.15  # policy default
    assert q1.limit == 6
    assert MemoryKind.SEMANTIC in q1.kinds and MemoryKind.EPISODIC in q1.kinds
    inactive = state(active=False)
    q3 = controller.build_query(inactive)
    assert "write failing test" not in q3.text


def test_into_state_populates_relevant_memories(tmp_path: Path) -> None:
    hit = RetrievedMemory(record=verified_record(), score=0.9, matched=["auth"])
    controller = MemoryController(FakeRetriever([hit]))
    original = state()
    updated = controller.into_state(original)
    assert updated.relevant_memories == [MemoryRef(memory_id="mem_fix", score=0.9)]
    assert updated.state_version == original.state_version + 1
    # input state never mutated (frozen transitions)
    assert original.relevant_memories == []
    assert original.state_version == 1


def test_into_state_with_working_view(tmp_path: Path) -> None:
    controller = MemoryController(FakeRetriever([]))
    view = WorkingMemoryView(notes=["goal: fix the auth module"])
    updated = controller.into_state(state(), working_view=view)
    assert updated.working_memory == view
    assert updated.relevant_memories == []


def test_retrieval_top_k_and_threshold_are_bounded(tmp_path: Path) -> None:
    hits = [
        RetrievedMemory(
            record=MemoryRecord(
                id=f"mem_{i}",
                kind=MemoryKind.SEMANTIC,
                content=f"fact {i}",
                provenance=MemoryProvenance(confidence=0.5),
            ),
            score=round(1.0 - i * 0.04, 2),
        )
        for i in range(20)
    ]
    controller = MemoryController(FakeRetriever(hits), RetrievalPolicy(top_k=3))
    updated = controller.into_state(state())
    assert len(updated.relevant_memories) == 3  # top-K bound
    assert [r.memory_id for r in updated.relevant_memories] == ["mem_0", "mem_1", "mem_2"]


def test_relevance_threshold_filters_hits(tmp_path: Path) -> None:
    low = RetrievedMemory(
        record=MemoryRecord(
            id="mem_low",
            kind=MemoryKind.SEMANTIC,
            content="weak match",
            provenance=MemoryProvenance(confidence=0.5),
        ),
        score=0.05,
    )
    high = RetrievedMemory(
        record=MemoryRecord(
            id="mem_high",
            kind=MemoryKind.SEMANTIC,
            content="strong match",
            provenance=MemoryProvenance(confidence=0.9),
        ),
        score=0.9,
    )
    controller = MemoryController(FakeRetriever([low, high]), RetrievalPolicy(min_score=0.5))
    updated = controller.into_state(state())
    assert [r.memory_id for r in updated.relevant_memories] == ["mem_high"]


def test_policy_rejects_empty_kinds_and_duplicate_kinds() -> None:
    with pytest.raises(ValidationError):
        RetrievalPolicy(kinds=[])
    with pytest.raises(ValidationError):
        RetrievalPolicy(kinds=[MemoryKind.SEMANTIC, MemoryKind.SEMANTIC])
    with pytest.raises(ValidationError):
        RetrievalPolicy(top_k=64)


def test_real_retrieval_flow_goal_to_brainstate(tmp_path: Path) -> None:
    st = store(tmp_path)
    st.write(verified_record())
    st.write(
        MemoryRecord(
            id="mem_ep1",
            kind=MemoryKind.EPISODIC,
            content="episode: previous auth fix used pytest",
            provenance=MemoryProvenance(source_task_id="task_prev", confidence=0.6),
        )
    )
    controller = MemoryController(Retriever(st))
    updated = controller.into_state(state())
    ids = [r.memory_id for r in updated.relevant_memories]
    assert "mem_fix" in ids and "mem_ep1" in ids  # semantic + episodic retrieved
    assert updated.relevant_memories[0].score >= updated.relevant_memories[-1].score


def test_superseded_memories_rank_below_current(tmp_path: Path) -> None:
    st = store(tmp_path)
    st.write(
        MemoryRecord(
            id="mem_old",
            kind=MemoryKind.SEMANTIC,
            content="auth module uses flask tokens",
            provenance=MemoryProvenance(confidence=0.6),
        )
    )
    supersede(
        st,
        old_id="mem_old",
        new=MemoryRecord(
            id="mem_new",
            kind=MemoryKind.SEMANTIC,
            content="auth module uses fastapi tokens",
            provenance=MemoryProvenance(confidence=0.95, verified=True),
        ),
    )
    controller = MemoryController(Retriever(st))
    hits = controller.retrieve_for_state(state())
    assert [h.record.id for h in hits][:2] == ["mem_new", "mem_old"]
    metrics = controller.last_metrics
    assert metrics is not None and metrics.superseded_returned == 1


def test_metrics_measure_retrieval(tmp_path: Path) -> None:
    st = store(tmp_path)
    st.write(verified_record())
    controller = MemoryController(Retriever(st))
    assert controller.last_metrics is None  # nothing measured before first use
    assert controller.hit_rate() == 0.0
    controller.retrieve_for_state(state())
    metrics = controller.last_metrics
    assert metrics is not None
    assert metrics.hit is True
    assert metrics.returned >= 1
    assert metrics.top_score >= metrics.avg_score > 0.0
    assert metrics.latency_ms >= 0.0
    assert metrics.duplicates == 0
    controller.retrieve_for_state(state())
    assert controller.hit_rate() == 1.0  # both retrievals hit


def test_metrics_detect_duplicate_contents(tmp_path: Path) -> None:
    dup = RetrievedMemory(
        record=MemoryRecord(
            id="mem_a",
            kind=MemoryKind.SEMANTIC,
            content="same content",
            provenance=MemoryProvenance(confidence=0.5),
        ),
        score=0.8,
    )
    dup2 = RetrievedMemory(
        record=MemoryRecord(
            id="mem_b",
            kind=MemoryKind.SEMANTIC,
            content="same content",
            provenance=MemoryProvenance(confidence=0.5),
        ),
        score=0.7,
    )
    controller = MemoryController(FakeRetriever([dup, dup2]))
    controller.retrieve_for_state(state())
    assert controller.last_metrics is not None
    assert controller.last_metrics.duplicates == 1


def test_predictions_are_never_persisted_as_verified(tmp_path: Path) -> None:
    """Observation ≠ Hypothesis ≠ Prediction ≠ Verified fact: a prediction
    (model claim) recorded as a semantic fact stays EXPLICITLY unverified."""
    st = store(tmp_path)
    stored = record_fact(st, task_id="task_p", fact="the build might pass now")
    assert stored is not None
    assert stored.provenance.verified is False
    assert stored.provenance.confidence <= 0.95
    # even a fabricated high-confidence claim is contract-rejected
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MemoryRecord(
            id="mem_fake",
            kind=MemoryKind.SEMANTIC,
            content="trust me",
            provenance=MemoryProvenance(confidence=1.0, verified=False),
        )
    # verified status requires a REAL verifier PASS artifact
    fake_pass = VerificationResult(
        id="verif_legacy",
        level=VerificationLevel.GOAL,
        task_id="task_p",
        verifier="legacy-placeholder",
        evidence=[EvidenceItem(claim="c", observed=True)],
        outcome=VerificationOutcome.PASS,
    )
    assert record_verified_fact(st, task_id="task_p", fact="x", goal_verdict=fake_pass) is None


def test_observations_preserve_provenance(tmp_path: Path) -> None:
    st = store(tmp_path)
    stored = record_fact(
        st, task_id="task_o", fact="terminal reported exit 0", source_event_id="evt_7"
    )
    assert stored is not None
    assert stored.provenance.source_task_id == "task_o"
    assert stored.provenance.source_event_id == "evt_7"


def test_trajectory_steps_from_state_are_ref_backed_and_ordered() -> None:

    s = state().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(id="cand_c1", kind=CandidateKind.EXECUTE, tool="terminal")
            ],
            "observations": [
                Observation(
                    id="obs_1",
                    task_id="task_g",
                    kind=ObservationKind.STEP_RESULT,
                    summary="ran ok",
                )
            ],
            "history": [],
        }
    )
    steps = trajectory_steps(s)
    assert [st.kind for st in steps] == [
        TrajectoryStepKind.ACTION,
        TrajectoryStepKind.OBSERVATION,
    ]
    assert steps[0].ref == "cand_c1" and steps[1].ref == "obs_1"
    assert steps[0].summary == "terminal"


def test_full_trajectory_replay_from_state(tmp_path: Path) -> None:
    """Goal → actions → observations → outcome, persisted as one replayable
    trajectory traceable to the mission."""
    s = state().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(id="cand_c1", kind=CandidateKind.EXECUTE, tool="terminal")
            ],
            "observations": [
                Observation(
                    id="obs_1",
                    task_id="task_g",
                    kind=ObservationKind.STEP_RESULT,
                    summary="ran ok",
                )
            ],
        }
    )
    st = store(tmp_path)
    outcome = "verified: file exists"
    record = record_trajectory(
        st,
        mission_id=s.mission_id,
        task_id=s.goal.id,
        goal_text=s.goal.objective,
        steps=trajectory_steps(s),
        outcome=outcome,
        verified=True,
        confidence=0.95,
    )
    assert record is not None
    assert record.mission_id == s.mission_id
    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    assert len(reloaded.trajectories()) == 1
    assert reloaded.trajectories()[0] == record


def test_historical_state_not_mutated_by_memory_writes(tmp_path: Path) -> None:
    """Memory writes never alter BrainState: recording is store-side only."""
    s = state().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(id="cand_c1", kind=CandidateKind.EXECUTE, tool="terminal")
            ],
        }
    )
    st = store(tmp_path)
    st.write(verified_record())
    record_trajectory(
        st,
        mission_id=s.mission_id,
        task_id=s.goal.id,
        goal_text="g",
        steps=trajectory_steps(s),
    )
    assert s.relevant_memories == []
    assert s.state_version == 1
    assert st.records() and st.trajectories()


def test_brain_memory_module_respects_import_boundary() -> None:
    source = inspect.getsource(__import__("nomadicos.brain.memory", fromlist=["x"]))
    # no import of the memory brick / runtime bricks from the brain module
    for forbidden in (
        r"from nomadicos\.memory",
        r"import nomadicos\.memory",
        r"from nomadicos\.executor",
        r"from nomadicos\.authority",
        r"from nomadicos\.orchestration",
    ):
        assert not re.search(forbidden, source), forbidden
