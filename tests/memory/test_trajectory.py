"""Sprint 5 — trajectory storage: structured, ref-backed, replayable."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nomadicos.contracts.memory import (
    MAX_TRAJECTORY_STEPS,
    MemoryProvenance,
    MemorySource,
    TrajectoryRecord,
    TrajectoryStep,
    TrajectoryStepKind,
)
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.trajectory import (
    make_step,
    record_trajectory,
    trajectory_id,
)

MISSION = "mission_x"
TASK = "task_t1"


def store_at(tmp_path: Path) -> JsonlMemoryStore:
    return JsonlMemoryStore(tmp_path / "memory.jsonl")


def sample_steps() -> list[TrajectoryStep]:
    return [
        make_step(TrajectoryStepKind.ACTION, "cand_c1", "run pytest"),
        make_step(TrajectoryStepKind.OBSERVATION, "obs_1", "test failed"),
        make_step(TrajectoryStepKind.FAILURE, "fail_1", "exit 1"),
        make_step(TrajectoryStepKind.RECOVERY, "evt_r1", "retried once"),
        make_step(TrajectoryStepKind.OUTCOME, "verif_1", "goal verified"),
    ]


def test_trajectory_id_is_deterministic() -> None:
    assert trajectory_id("m", "t") == trajectory_id("m", "t")
    assert trajectory_id("m", "t") != trajectory_id("m", "t2")
    assert trajectory_id("m", "t").startswith("mem-traj-")


def test_trajectory_step_validation() -> None:
    step = TrajectoryStep(kind=TrajectoryStepKind.ACTION, ref="cand_1", summary="s")
    assert step.ref == "cand_1"
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        TrajectoryStep(kind=TrajectoryStepKind.ACTION, ref="  ")
    # bounded: over-long summaries are truncated, never stored verbatim
    truncated = TrajectoryStep(kind=TrajectoryStepKind.ACTION, ref="c", summary="x" * 400)
    assert len(truncated.summary) == 200
    assert len(truncated.ref) <= 128


def test_trajectory_record_bounds() -> None:
    steps = [
        TrajectoryStep(kind=TrajectoryStepKind.ACTION, ref=f"c{i}", summary="s")
        for i in range(MAX_TRAJECTORY_STEPS + 1)
    ]
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        TrajectoryRecord(
            id="mem-traj-x",
            mission_id="m",
            task_id="t",
            steps=steps,
            provenance=MemoryProvenance(confidence=0.5),
        )
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        TrajectoryRecord(
            id="mem-traj-x",
            mission_id=" ",
            task_id="t",
            provenance=MemoryProvenance(confidence=0.5),
        )


def test_trajectory_replayable_roundtrip(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = record_trajectory(
        store,
        mission_id=MISSION,
        task_id=TASK,
        goal_text="fix the auth module",
        steps=sample_steps(),
        outcome="verified: goal predicates pass",
        verified=True,
        confidence=0.95,
    )
    assert record is not None
    raw = (tmp_path / "memory.jsonl").read_text(encoding="utf-8")
    reloaded = JsonlMemoryStore(tmp_path / "memory.jsonl")
    back = reloaded.trajectories()[0]
    assert back.model_dump(mode="json") == record.model_dump(mode="json")
    assert back.steps[0].kind is TrajectoryStepKind.ACTION
    assert back.steps[1].ref == "obs_1"
    assert raw.count("storage_type") >= 1  # one JSONL envelope line


def test_trajectory_preserves_structure_not_only_summary(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = record_trajectory(
        store, mission_id=MISSION, task_id=TASK, goal_text="g", steps=sample_steps()
    )
    assert record is not None
    kinds = [s.kind for s in record.steps]
    assert kinds == [
        TrajectoryStepKind.ACTION,
        TrajectoryStepKind.OBSERVATION,
        TrajectoryStepKind.FAILURE,
        TrajectoryStepKind.RECOVERY,
        TrajectoryStepKind.OUTCOME,
    ]
    # every step traceable to a source ref; provenance carries evidence refs
    assert all(s.ref for s in record.steps)
    assert record.provenance.evidence_refs[0] == "cand_c1" or record.provenance.evidence_refs


def test_trajectory_idempotent_upsert(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    first = record_trajectory(
        store, mission_id=MISSION, task_id=TASK, goal_text="g", steps=sample_steps()
    )
    second = record_trajectory(
        store, mission_id=MISSION, task_id=TASK, goal_text="g", steps=sample_steps()
    )
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(store.trajectories()) == 1


def test_recorder_requires_real_content(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert (
        record_trajectory(store, mission_id="", task_id="t", goal_text="g", steps=sample_steps())
        is None
    )
    assert (
        record_trajectory(store, mission_id="m", task_id="", goal_text="g", steps=sample_steps())
        is None
    )
    assert record_trajectory(store, mission_id="m", task_id="t", goal_text="g", steps=[]) is None


def test_recorder_is_fail_safe(tmp_path: Path) -> None:
    class _Broken:
        def upsert_trajectory(self, trajectory):
            raise OSError("disk full")

    assert (
        record_trajectory(
            _Broken(), mission_id="m", task_id="t", goal_text="g", steps=sample_steps()
        )
        is None
    )


def test_trajectory_scrubs_secrets(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    steps = [make_step(TrajectoryStepKind.OBSERVATION, "obs_1", "used password=hunter2")]
    record = record_trajectory(store, mission_id=MISSION, task_id=TASK, goal_text="g", steps=steps)
    assert record is not None
    assert "hunter2" not in record.steps[0].summary


def test_trajectory_source_type_and_provenance(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record = record_trajectory(
        store,
        mission_id=MISSION,
        task_id=TASK,
        goal_text="g",
        steps=sample_steps(),
        source_event_id="evt_9",
    )
    assert record is not None
    assert record.provenance.source_type is MemorySource.TRAJECTORY
    assert record.provenance.source_task_id == TASK
    assert record.provenance.source_event_id == "evt_9"
    assert record.verified is False  # recorder never invents verification
    assert record.provenance.confidence <= 0.6


def test_trajectory_module_is_data_only() -> None:
    source = inspect.getsource(__import__("nomadicos.memory.trajectory", fromlist=["x"]))
    for forbidden in (
        "nomadicos.authority",
        "nomadicos.executor",
        "nomadicos.orchestration",
        "nomadicos.router",
        "import langgraph",
        "AuthorizedAction",
        "TaskStatus",
        "subprocess",
        "Popen",
        "os.system",
    ):
        assert forbidden not in source


def test_trajectory_survives_corrupt_lines(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    record_trajectory(store, mission_id="m", task_id="t", goal_text="g", steps=sample_steps())
    path = tmp_path / "memory.jsonl"
    path.write_text("{ not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    reloaded = JsonlMemoryStore(path)
    assert reloaded.dropped_invalid_lines == 1
    assert len(reloaded.trajectories()) == 1
