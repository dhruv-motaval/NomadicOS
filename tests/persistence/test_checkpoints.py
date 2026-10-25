"""Phase 13B durable checkpoint tests: file-adapter round-trips, fresh
restore, task isolation, corruption handling, bounds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from nomadicos.kernel.config import PersistenceConfig
from nomadicos.persistence import PersistenceCorrupt
from nomadicos.persistence.checkpoints import (
    MAX_CHECKPOINTS_PER_THREAD,
    FileCheckpointSaver,
    make_durable_saver,
)


class _S(TypedDict, total=False):
    count: int


def _bump(state: _S) -> dict:
    return {"count": state["count"] + 1}


def _builder():
    builder = StateGraph(_S)
    builder.add_node("n", _bump)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder


def test_checkpoint_round_trip_across_fresh_instance(tmp_path: Path) -> None:
    graph = _builder().compile(checkpointer=FileCheckpointSaver(tmp_path))
    graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:task_1"}})
    fresh = _builder().compile(checkpointer=FileCheckpointSaver(tmp_path))
    loaded = fresh.get_state({"configurable": {"thread_id": "nomadic:task_1"}})
    assert loaded.values.get("count") == 1


def test_thread_isolation(tmp_path: Path) -> None:
    graph = _builder().compile(checkpointer=FileCheckpointSaver(tmp_path))
    graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:task_a"}})
    loaded = graph.get_state({"configurable": {"thread_id": "nomadic:task_B"}})
    assert loaded.values == {}


def test_corrupt_snapshot_fails_closed(tmp_path: Path) -> None:
    saver = FileCheckpointSaver(tmp_path)
    graph = _builder().compile(checkpointer=saver)
    graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:task_1"}})
    snapshot = tmp_path / "checkpoints" / "langgraph-threads.json"
    snapshot.write_text("{broken json", encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        FileCheckpointSaver(tmp_path)


def test_schema_version_mismatch_fails_closed(tmp_path: Path) -> None:
    snapshot = tmp_path / "checkpoints" / "langgraph-threads.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps({"schema_version": 99, "kind": "langgraph_checkpoints"}),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="version"):
        FileCheckpointSaver(tmp_path)


def test_unknown_kind_fails_closed(tmp_path: Path) -> None:
    store = tmp_path / "checkpoints" / "langgraph-threads.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"schema_version": 1, "kind": "unknown_kind"}), encoding="utf-8")
    with pytest.raises(PersistenceCorrupt):
        FileCheckpointSaver(tmp_path)


def test_make_durable_saver_selects_file_adapter(tmp_path: Path) -> None:
    cfg = PersistenceConfig(state_dir=str(tmp_path))
    saver = make_durable_saver(cfg)
    assert type(saver).__name__ == "FileCheckpointSaver"


def test_checkpoint_count_is_bounded(tmp_path: Path) -> None:
    saver = FileCheckpointSaver(tmp_path)
    graph = _builder().compile(checkpointer=saver)
    for _ in range(MAX_CHECKPOINTS_PER_THREAD + 6):
        graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:task_1"}})
    envelope = json.loads(
        (tmp_path / "checkpoints" / "langgraph-threads.json").read_text(encoding="utf-8")
    )
    # bounded: the snapshot file cannot grow with unbounded task length
    for ns_map in envelope["storage"]["__map__"]:
        ns, cps = ns_map
        assert len(cps["__map__"]) <= MAX_CHECKPOINTS_PER_THREAD


def test_no_success_or_authority_content_in_saver(tmp_path: Path) -> None:
    import inspect

    from nomadicos.persistence import checkpoints as module

    source = inspect.getsource(module)
    for token in (
        "TaskStatus.SUCCESS",
        "AuthorizedAction(",
        "grant_full",
        "revoke_all",
        "answer_conflict",
        "subprocess",
        "Popen",
        "os.system",
        "urllib",
        "requests",
    ):
        assert token not in source, token
