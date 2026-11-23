"""Plan meaning checks + bounded repair loop in the decomposer."""

from __future__ import annotations

import json

import pytest

from nomadicos.brain.cognition import CognitiveRole
from nomadicos.brain.decomposer import ModelDecomposer, parse_decomposition
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.plan_quality import check_plan
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate

from .conftest import FakeGenerator

PRED = {"type": "file_exists", "path": "out.txt"}
GOAL = Goal(id="g1", objective="Produce out.txt", predicates=[parse_predicate(PRED)])


def _task(tid: str, desc: str, deps: list[str] | None = None, **extra: object) -> dict:
    return {"id": tid, "description": desc, "depends_on": deps or [], **extra}


def _graph(tasks: list[dict], subgoals: list[dict] | None = None):
    text = json.dumps({"subgoals": subgoals or [], "tasks": tasks})
    return parse_decomposition(text, goal_id=GOAL.id).task_graph


GOOD = [
    _task("t1", "Research the format", worker_role="research"),
    _task("t2", "Write out.txt", ["t1"], worker_role="coding", success_predicate=PRED),
]


def test_sensible_plan_passes() -> None:
    assert check_plan(_graph(GOOD), GOAL, require_coverage=True) == []


def test_uncovered_goal_predicate_only_flagged_when_required() -> None:
    graph = _graph([_task("t1", "Do it")])
    assert check_plan(graph, GOAL) == []
    assert any("success_predicate" in p for p in check_plan(graph, GOAL, require_coverage=True))


def test_too_many_tasks_flagged() -> None:
    tasks = [_task(f"t{i}", f"distinct step number {i} alpha{i}") for i in range(8)]
    assert any("too many" in p for p in check_plan(_graph(tasks), GOAL))


def test_duplicate_description_and_action_flagged() -> None:
    tasks = [
        _task("t1", "Write the file", tool="fs", arguments={"p": 1}),
        _task("t2", "write the  FILE!", tool="fs", arguments={"p": 1}),
    ]
    problems = check_plan(_graph(tasks), GOAL)
    assert any("same description" in p for p in problems)
    assert any("same action" in p for p in problems)


def test_coding_must_depend_on_research_directly_or_transitively() -> None:
    tasks = [
        _task("r", "Look up", worker_role="research"),
        _task("c", "Code", worker_role="coding"),
    ]
    assert any("must depend on a research" in p for p in check_plan(_graph(tasks), GOAL))
    tasks = [
        _task("r", "Look up", worker_role="research"),
        _task("m", "Middle step", ["r"]),
        _task("c", "Code", ["m"], worker_role="coding"),
    ]
    assert check_plan(_graph(tasks), GOAL) == []


def test_run_and_save_in_one_task_flagged() -> None:
    graph = _graph([_task("t1", "Run sorted.py with python and save its output to output.txt")])
    assert any("two tasks" in p for p in check_plan(graph, GOAL))
    graph = _graph(
        [_task("t1", "Run sorted.py"), _task("t2", "Save the output to output.txt", ["t1"])]
    )
    assert check_plan(graph, GOAL) == []


def test_unknown_predicate_type_flagged() -> None:
    graph = _graph([_task("t1", "Look up", success_predicate={"type": "research_complete"})])
    assert check_plan(graph, GOAL) == []  # no set supplied: not checked
    known = frozenset({"file_exists"})
    assert any(
        "research_complete" in p for p in check_plan(graph, GOAL, known_predicate_types=known)
    )


BAD = json.dumps(
    {
        "tasks": [
            _task("c", "Code", worker_role="coding"),
            _task("r", "Look up", worker_role="research"),
        ]
    }
)
OK = json.dumps({"tasks": GOOD})


def _state() -> BrainState:
    return BrainState.for_goal(GOAL, mission_id="m_quality")


async def test_decomposer_repairs_bad_plan_with_feedback() -> None:
    gen = FakeGenerator()
    gen.script(CognitiveRole.DECOMPOSER, BAD, OK)
    result = await ModelDecomposer(gen).decompose(_state())  # type: ignore[arg-type]
    assert [n.id for n in result.task_graph.nodes] == ["t1", "t2"]
    assert "fix_these_plan_errors" not in gen.calls[0].payload
    assert any("research" in e for e in gen.calls[1].payload["fix_these_plan_errors"])


async def test_decomposer_fails_closed_after_repairs_exhausted() -> None:
    gen = FakeGenerator()
    gen.script(CognitiveRole.DECOMPOSER, BAD)
    with pytest.raises(ModelOutputRejected, match="implausible plan"):
        await ModelDecomposer(gen, max_repairs=2).decompose(_state())  # type: ignore[arg-type]
    assert len(gen.calls) == 3


def test_read_only_worker_cannot_carry_a_command_output_predicate() -> None:
    needs_run = {"type": "stdout_contains", "text": "ALL_PASS"}
    for role in ("testing", "research"):
        tasks = [
            _task("t1", "Write out.txt", worker_role="coding", success_predicate=PRED),
            _task("t2", "Check it", ["t1"], worker_role=role, success_predicate=needs_run),
        ]
        problems = check_plan(_graph(tasks), GOAL)
        mine = [x for x in problems if "cannot prove" in x]
        assert len(mine) == 1 and "'t2'" in mine[0] and "execute" in mine[0]
    # the same check on an execute task is a normal, provable step
    tasks = [
        _task("t1", "Write out.txt", worker_role="coding", success_predicate=PRED),
        _task("t2", "Run the check", ["t1"], worker_role="execute", success_predicate=needs_run),
    ]
    assert not [x for x in check_plan(_graph(tasks), GOAL) if "cannot prove" in x]
    # file evidence from a testing task is not blocked by this rule
    tasks[1] = _task("t2", "Review", ["t1"], worker_role="testing", success_predicate=PRED)
    assert check_plan(_graph(tasks), GOAL) == []
