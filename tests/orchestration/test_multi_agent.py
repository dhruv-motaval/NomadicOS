"""Multi-agent missions (SPEC §46): split, run agents in parallel, verify."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from helpers import make_app, write_json

from nomadicos.contracts.action import ActionProposal
from nomadicos.contracts.core import TaskStatus
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.kernel.events import EventType

GOAL = "Make the three report files"
PARENT_PREDICATES = [{"type": "file_exists", "path": p} for p in ("a.txt", "b.txt", "c.txt")]


def decomposition(*, c_depends: bool = True) -> str:
    def task(tid: str, name: str, deps: list[str]) -> dict:
        return {
            "id": tid,
            "description": f"Write file {name}",
            "depends_on": deps,
            "success_predicate": {"type": "file_exists", "path": name},
        }

    return json.dumps(
        {
            "tasks": [
                task("t1", "a.txt", []),
                task("t2", "b.txt", []),
                task("t3", "c.txt", ["t1", "t2"] if c_depends else []),
            ]
        }
    )


def scripts(a_reply: str | None = None) -> list[tuple[str, str]]:
    # the c.txt rule goes first: c's prompt also quotes the parts it builds on
    return [
        ("task decomposer", decomposition()),
        ("Write file c.txt", write_json("c.txt", "C")),
        ("Write file a.txt", a_reply or write_json("a.txt", "A")),
        ("Write file b.txt", write_json("b.txt", "B")),
    ]


class CountingEngine(MockEngine):
    """Mock engine that records how many generations overlap."""

    def __init__(self, inner: MockEngine) -> None:
        super().__init__(default_response=inner.default_response)
        self.rules = inner.rules
        self.in_flight = 0
        self.peak = 0

    async def generate(self, request):  # type: ignore[override]
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.05)
            return await super().generate(request)
        finally:
            self.in_flight -= 1


async def test_goal_runs_as_parallel_agents_and_is_verified_over_all_of_them(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, scripts=scripts())
    engine = CountingEngine(app.engines["mock"])
    app.engines["mock"] = engine
    summary = await app.run_agents(GOAL, task_id="mission", predicates=PARENT_PREDICATES)

    assert summary.status is TaskStatus.SUCCESS  # parent verifier PASS over all agents
    assert [a["status"] for a in summary.agents] == ["SUCCESS"] * 3
    shared = tmp_path / "ws" / "mission"  # every agent wrote into the parent's folder
    assert {p.name for p in shared.iterdir()} >= {"a.txt", "b.txt", "c.txt"}
    assert engine.peak >= 2  # the two independent agents overlapped

    events = app.log.events()
    created = {e.task_id: i for i, e in enumerate(events) if e.type is EventType.TASK_CREATED}
    completed = {e.task_id: i for i, e in enumerate(events) if e.type is EventType.TASK_COMPLETED}
    # the dependent agent started only after both of its inputs finished
    assert created["mission-t3"] > completed["mission-t1"]
    assert created["mission-t3"] > completed["mission-t2"]
    await app.aclose()


async def test_failed_part_is_retried_once_then_dependents_are_skipped(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=scripts(a_reply='{"finished": true}'),  # agent for a.txt never writes it
        config_over={"budget": {"max_recoveries": 1}},
    )
    summary = await app.run_agents(GOAL, task_id="m2", predicates=PARENT_PREDICATES)
    by_part = {a["agent"]: a for a in summary.agents}
    assert by_part["m2-t1"]["status"] in {"FAILED", "BLOCKED"}
    assert by_part["m2-t1"]["attempts"] == 2
    assert by_part["m2-t2"]["status"] == "SUCCESS"  # a sibling's failure never stops it
    assert by_part["m2-t3"]["status"] == "SKIPPED"
    assert summary.status is not TaskStatus.SUCCESS
    await app.aclose()


async def test_a_goal_that_cannot_be_split_runs_as_one_agent(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[("task decomposer", "not json"), ("Propose", write_json("a.txt", "A"))],
    )
    summary = await app.run_agents(
        "Write a.txt", task_id="solo", predicates=[{"type": "file_exists", "path": "a.txt"}]
    )
    assert summary.agents == []
    assert summary.status is TaskStatus.SUCCESS
    await app.aclose()


async def test_two_agents_never_change_one_file_at_the_same_time(tmp_path: Path) -> None:
    app = make_app(tmp_path, config_over={"budget": {"write_lease_timeout_s": 0.2}})
    ws = tmp_path / "ws" / "shared"

    def write(task: str) -> ActionProposal:
        return ActionProposal(
            task_id=task,
            step_id="s",
            model_id="m",
            tool="filesystem",
            operation="write",
            args={"path": "notes.txt", "content": task},
        )

    await app.runtime.acquire_for(write("agent-a"), "agent-a", ws)
    await app.runtime.acquire_for(write("agent-a"), "agent-a", ws)  # re-entrant for its owner
    with pytest.raises(ResourceUnavailable, match="another agent"):
        await app.runtime.acquire_for(write("agent-b"), "agent-b", ws)
    app.runtime.release_desktop("agent-a")  # agent-a's run ended
    await app.runtime.acquire_for(write("agent-b"), "agent-b", ws)
    app.runtime.release_desktop("agent-b")
    await app.aclose()
