"""Phase 13C restart/resume lifecycle: deterministic simulated restart using
the REAL durable checkpoint + task-state stores (no in-memory shortcuts)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.memory import MemoryQuery
from nomadicos.persistence import PersistenceCorrupt

GOAL = "Create file report.txt containing DONE"
PRED: list[dict[str, Any]] = [{"type": "file_exists", "path": "report.txt"}]
SCRIPT = [("report.txt", write_json("report.txt", "DONE"))]


async def test_completed_task_restores_as_data_without_reexecution(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=SCRIPT)
    summary_a = await app_a.run_goal(GOAL, predicates=PRED)
    assert summary_a.status is TaskStatus.SUCCESS
    task_id = summary_a.task_id
    await app_a.aclose()

    # new process: fresh app over the same state_dir
    app_b = make_app(tmp_path)
    tasks = app_b.list_durable_tasks()
    entry = next(t for t in tasks if t["task_id"] == task_id)
    assert entry["status"] == "SUCCESS"
    resumed = await app_b.resume_task(task_id)
    assert resumed.status is TaskStatus.SUCCESS
    # no re-execution, no new events, no duplicate SUCCESS
    assert app_b.log.events(task_id) == []
    record = app_b.task_store.load_task(task_id)
    assert record is not None and record.status is TaskStatus.SUCCESS
    assert any((tmp_path / "ws").rglob("report.txt"))


async def test_owner_wait_restores_without_auto_answer(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=[("secret", write_json("secret.txt", "X"))])
    app_a.store.add_instruction("never touch the secret file", "secret")
    summary = await app_a.run_goal(
        "Create file secret.txt containing X",
        predicates=[{"type": "file_exists", "path": "secret.txt"}],
    )
    assert summary.waiting_owner()
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path, scripts=[("secret", write_json("secret.txt", "X"))])
    restored = await app_b.resume_task(task_id)
    assert restored.waiting_owner()
    assert not any((tmp_path / "ws").rglob("secret.txt"))  # never executed while asking
    completed = await app_b.resume_owner(task_id, "ALLOW")
    assert completed.status is TaskStatus.SUCCESS, completed.outcome_note
    assert any((tmp_path / "ws").rglob("secret.txt"))


async def test_epoch_change_while_stopped_blocks_stale_action(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=[("secret", write_json("secret.txt", "X"))])
    app_a.store.add_instruction("never touch the secret file", "secret")
    summary = await app_a.run_goal(
        "Create file secret.txt containing X",
        predicates=[{"type": "file_exists", "path": "secret.txt"}],
    )
    assert summary.waiting_owner()
    task_id = summary.task_id
    await app_a.aclose()

    app_b = make_app(tmp_path, scripts=[("secret", write_json("secret.txt", "X"))])
    app_b.store.revoke_all()  # authority changed while the task was stopped
    summary = await app_b.resume_owner(task_id, "ALLOW")
    assert summary.status.value != "SUCCESS"
    assert not any((tmp_path / "ws").rglob("secret.txt"))


async def test_resume_unknown_task_fails_safely(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with pytest.raises(PersistenceCorrupt):
        await app.resume_task("task_none00000000000000")


def test_corrupt_checkpoint_fails_app_construction(tmp_path: Path) -> None:
    make_app(tmp_path, scripts=SCRIPT)
    snapshot = tmp_path / "state" / "checkpoints" / "langgraph-threads.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        make_app(tmp_path)


async def test_memory_reopens_without_duplicate_records(tmp_path: Path) -> None:
    app_a = make_app(tmp_path, scripts=SCRIPT)
    await app_a.run_goal(GOAL, predicates=PRED)
    records_before = len(app_a.memory_store.query(MemoryQuery(text="report", limit=32)))
    app_b = make_app(tmp_path)
    records_after = len(app_b.memory_store.query(MemoryQuery(text="report", limit=32)))
    assert records_before == records_after
    await app_a.aclose()
