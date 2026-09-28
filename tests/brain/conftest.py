"""Fixtures for brain foundation + MAP core tests (Sprint 1, Sprint 2)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count

import pytest

from nomadicos.brain.cognition import CognitiveRequest, CognitiveRole
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate


class FakeGenerator:
    """Deterministic ``StructuredGenerator`` fake (no model, no network).

    Scripted FIFO responses per cognitive role; the last scripted text
    sticks. Records every call for provenance assertions.
    """

    def __init__(self) -> None:
        self.scripted: dict[str, list[str]] = {}
        self.calls: list[CognitiveRequest] = []

    def script(self, role: CognitiveRole, *texts: str) -> None:
        self.scripted.setdefault(role.value, []).extend(texts)

    async def generate_structured(self, request: CognitiveRequest) -> str:
        self.calls.append(request)
        texts = self.scripted.get(request.role.value)
        if not texts:
            raise AssertionError(f"unexpected {request.role.value} model call")
        return texts.pop(0) if len(texts) > 1 else texts[0]


@pytest.fixture()
def goal() -> Goal:
    """A minimal, fully explicit (deterministic) owner goal."""
    return Goal(
        id="task_goal1",
        objective="Produce out.txt",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )


@pytest.fixture()
def chain(goal: Goal) -> TaskGraph:
    """Linear graph a -> b -> c for the fixture goal."""
    nodes = [
        TaskNode(id="a", description="Task A"),
        TaskNode(id="b", description="Task B", depends_on=["a"]),
        TaskNode(id="c", description="Task C", depends_on=["b"]),
    ]
    return TaskGraph.build(goal_id=goal.id, nodes=nodes)


@pytest.fixture()
def state(goal: Goal, chain: TaskGraph) -> BrainState:
    """Canonical test state: explicit identity, deterministic defaults."""
    return BrainState.for_goal(goal, task_graph=chain, mission_id="mission_test")


@pytest.fixture()
def fresh_goal(goal: Goal) -> Goal:
    """A goal with an EMPTY (not yet decomposed) graph."""
    return goal


@pytest.fixture()
def empty_state(goal: Goal) -> BrainState:
    """State whose graph is not yet decomposed (MAP entry point)."""
    return BrainState.for_goal(
        Goal(id="task_map1", objective="Produce out.txt"), mission_id="mission_map"
    )


@pytest.fixture()
def fixed_now() -> Callable[[], datetime]:
    """Injected wall clock: same timestamp for every minted event."""
    return lambda: datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)


def event_id_factory() -> Callable[[], str]:
    """Factory for FRESH deterministic event-id sequences (one per run)."""
    counter = count(1)

    def _next() -> str:
        return f"evt_{next(counter):04d}"

    return _next
