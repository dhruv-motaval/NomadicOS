"""HARDWARE golden-set eval: does a real model split goals sensibly?

Run (llama-server serving MODEL_ID on :8080):
    pytest -m hardware tests/hardware/test_decomposition_quality_live.py -s

Measures RAW first-attempt plans (``max_repairs=0``), so the pass rate is the
model's own quality, not the repair loop's. Properties, not exact graphs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx
import pytest

from nomadicos.brain.decomposer import ModelDecomposer
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.plan_quality import check_plan
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal
from nomadicos.contracts.worker import WorkerRole
from nomadicos.inference.llama_cpp import LlamaCppEngine
from nomadicos.inference.structured import EngineStructuredGenerator

pytestmark = pytest.mark.hardware

BASE_URL = "http://127.0.0.1:8080"
MODEL_ID = os.environ.get("NOMADIC_EVAL_MODEL", "models/Spark-X2.5-4B-uncensored-Q4_K_M.gguf")
RUNS = int(os.environ.get("NOMADIC_EVAL_RUNS", "3"))
MIN_PASS_RATE = 0.5  # shortcut: smoke bar only, raise once per-model baselines exist

R, C, T = WorkerRole.RESEARCH, WorkerRole.CODING, WorkerRole.TESTING


@dataclass(frozen=True)
class Golden:
    name: str
    objective: str
    predicates: list[dict]
    min_tasks: int = 1
    max_tasks: int = 4
    roles: set[WorkerRole] = field(default_factory=set)  # must all appear
    edges: list[tuple[WorkerRole, WorkerRole]] = field(default_factory=list)  # (later, earlier)


def _file(path: str) -> dict:
    return {"type": "file_exists", "path": path}


GOLDEN = [
    Golden("single_file", "Create out.txt containing hello", [_file("out.txt")], 1, 2),
    Golden(
        "two_files",
        "Create a.txt and b.txt, each containing hello",
        [_file("a.txt"), _file("b.txt")],
        2,
        5,
    ),
    Golden(
        "research_then_code",
        "Research how the Python json module sorts keys, then write sorted.py that "
        "prints a sorted-keys JSON object",
        [_file("sorted.py")],
        2,
        5,
        {R, C},
        [(C, R)],
    ),
    Golden(
        "fix_then_test",
        "Fix multiply in calculator.py to return a * b, then run the tests",
        [_file("calculator.py")],
        2,
        5,
        {C, T},
        [(T, C)],
    ),
    Golden("copy_file", "Copy seed.txt to copy.txt", [_file("copy.txt")], 1, 3),
]


def _depends_on_role(graph: TaskGraph, node_id: str, role: WorkerRole) -> bool:
    stack, seen = list(graph.node(node_id).depends_on), set()
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if graph.node(dep).worker_role is role:
            return True
        stack.extend(graph.node(dep).depends_on)
    return False


def judge(g: Golden, graph: TaskGraph, goal: Goal) -> list[str]:
    problems = check_plan(graph, goal, require_coverage=True)
    n = len(graph.nodes)
    if not g.min_tasks <= n <= g.max_tasks:
        problems.append(f"{n} tasks, expected {g.min_tasks}-{g.max_tasks}")
    have = {x.worker_role for x in graph.nodes}
    problems += [f"missing role {r.value}" for r in sorted(g.roles - have)]
    for later, earlier in g.edges:
        for node in graph.nodes:
            if node.worker_role is later and not _depends_on_role(graph, node.id, earlier):
                problems.append(f"{later.value} task {node.id!r} lacks {earlier.value} dependency")
    return problems


async def test_decomposition_quality_golden_set() -> None:
    client = httpx.AsyncClient(base_url=BASE_URL, timeout=180.0)
    engine = LlamaCppEngine(client=client)
    try:
        try:
            health = await engine.health()
        except Exception as exc:
            pytest.skip(f"llama-server unreachable at {BASE_URL}: {exc}")
        assert MODEL_ID in health.models, f"{MODEL_ID!r} not served; got {health.models}"
        generator = EngineStructuredGenerator(engine, MODEL_ID, max_tokens=1024, temperature=0.2)
        decomposer = ModelDecomposer(generator, max_repairs=0)  # type: ignore[arg-type]
        passed = total = 0
        for g in GOLDEN:
            goal = Goal.from_spec(g.objective, predicates=g.predicates)
            ok = 0
            for i in range(RUNS):
                state = BrainState.for_goal(goal, mission_id=f"eval_{g.name}_{i}")
                try:
                    problems = judge(g, (await decomposer.decompose(state)).task_graph, goal)
                except ModelOutputRejected as exc:
                    problems = [f"rejected: {exc}"]
                ok += not problems
                if problems:
                    print(f"  [{g.name} run {i}] {problems}")
            print(f"{g.name}: {ok}/{RUNS}")
            passed, total = passed + ok, total + RUNS
        print(f"model={MODEL_ID} sensible plans: {passed}/{total}")
        assert passed / total >= MIN_PASS_RATE
    finally:
        await client.aclose()
