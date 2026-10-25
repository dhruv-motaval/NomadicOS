"""Phase 13B PostgreSQL checkpoint-saver integration tests (SPEC 34).

Integration-marked: requires a reachable PostgreSQL per docker-compose.dev.yml.
Skips cleanly when unavailable; never faked with the file adapter.
"""

from __future__ import annotations

import os
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from nomadicos.persistence.checkpoints import PostgresCheckpointSaver
from nomadicos.persistence.errors import PersistenceUnavailable

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


class _S(TypedDict, total=False):
    count: int


def _store() -> PostgresCheckpointSaver:
    try:
        return PostgresCheckpointSaver(TEST_DSN, connect_timeout_s=2)
    except PersistenceUnavailable as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc.message[:100]}")


def _builder():
    builder = StateGraph(_S)
    builder.add_node("n", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder


def test_postgres_checkpoint_round_trip_and_fresh_restore() -> None:
    saver = _store()
    graph = _builder().compile(checkpointer=saver)
    graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:pg_task_1"}})
    fresh = _builder().compile(checkpointer=_store())
    loaded = fresh.get_state({"configurable": {"thread_id": "nomadic:pg_task_1"}})
    assert loaded.values.get("count") == 1


def test_postgres_checkpoint_thread_isolation() -> None:
    saver = _store()
    graph = _builder().compile(checkpointer=saver)
    graph.invoke({"count": 0}, {"configurable": {"thread_id": "nomadic:pg_task_a"}})
    loaded = graph.get_state({"configurable": {"thread_id": "nomadic:pg_task_B"}})
    assert loaded.values == {}


def test_postgres_unavailable_is_structured() -> None:
    with pytest.raises(PersistenceUnavailable):
        PostgresCheckpointSaver(
            "postgresql://nomadicos:nomadicos@localhost:9999/nomadicos", connect_timeout_s=1
        )
