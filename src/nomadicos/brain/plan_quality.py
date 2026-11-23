"""Meaning checks for a decomposition (beyond schema validity).

``TaskGraph`` already guarantees structure (ids, cycles, dependencies).
This module asks the next question: *could this plan plausibly achieve the
goal?* Deterministic, model-free, and side-effect free: it returns a list of
human-readable problems, empty when the plan passes. The decomposer feeds
them back to the model for a bounded repair, then fails closed.
"""

from __future__ import annotations

import re

from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.contracts.core import Goal, GoalPredicate
from nomadicos.contracts.worker import WorkerRole

#: a plan may use this many tasks regardless of goal size
BASE_TASKS = 3
#: ...plus this many per distinct goal predicate (shortcut: linear heuristic,
#: tune from the golden-set eval if it rejects good plans)
TASKS_PER_PREDICATE = 2


def _leaves(pred: GoalPredicate | None) -> list[str]:
    if pred is None:
        return []
    if pred.predicate is not None:
        return [pred.predicate.model_dump_json()]
    assert pred.group is not None
    return [leaf for child in pred.group.children for leaf in _leaves(child)]


def _leaf_types(pred: GoalPredicate | None) -> list[str]:
    if pred is None:
        return []
    if pred.predicate is not None:
        return [pred.predicate.type]
    assert pred.group is not None
    return [t for child in pred.group.children for t in _leaf_types(child)]


def _ancestors(graph: TaskGraph, node_id: str) -> set[str]:
    seen: set[str] = set()
    stack = list(graph.node(node_id).depends_on)
    while stack:
        dep = stack.pop()
        if dep not in seen:
            seen.add(dep)
            stack.extend(graph.node(dep).depends_on)
    return seen


#: predicate types whose proof is a command's output/exit. Only an EXECUTE task
#: runs commands (research/testing workers return findings and run nothing).
#: shortcut: hard-coded because the brain must not import verification; keep in
#: sync with verification.predicates when an execution-evidence type is added
_NEEDS_COMMAND_RUN = frozenset(
    {"stdout_contains", "stdout_equals", "stderr_contains", "exit_code_equals", "tests_pass"}
)

# shortcut: keyword heuristic, English only; upgrade when the golden eval shows misses
_RUN_AND_SAVE = re.compile(
    r"\b(run|execute)\b.*\b(and|then)\b.*\b(save|write|store)\b", re.IGNORECASE
)


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def check_plan(
    graph: TaskGraph,
    goal: Goal,
    *,
    require_coverage: bool = False,
    known_predicate_types: frozenset[str] | None = None,
) -> list[str]:
    """Return everything wrong with the plan's meaning (empty = sensible).

    ``require_coverage`` demands each goal predicate appear as some task's
    own success_predicate. Off by default: the GoalVerifier proves goal
    predicates independently, so the runtime does not need tasks to repeat
    them; the golden eval turns it on.
    """
    problems: list[str] = []
    nodes = graph.nodes

    # 0. A predicate type the verifier does not know can never be satisfied: the
    # step would stall forever (seen live: models invent "research_complete").
    # The caller supplies the set (brain must not import verification).
    known = known_predicate_types or frozenset()
    for n in nodes if known else ():
        for t in _leaf_types(n.success_predicate):
            if t not in known:
                problems.append(
                    f"task {n.id!r} uses unknown predicate type {t!r}; "
                    f"use one of {sorted(known)} or omit success_predicate"
                )

    # 1. Optionally, every goal predicate is some task's own check.
    wanted = {leaf for gp in goal.predicates for leaf in _leaves(gp)}
    covered = {leaf for n in nodes for leaf in _leaves(n.success_predicate)}
    for leaf in sorted(wanted - covered if require_coverage else ()):
        problems.append(f"no task has the goal's success_predicate {leaf}")

    # 2. Proportionate size.
    limit = BASE_TASKS + TASKS_PER_PREDICATE * len(wanted)
    if len(nodes) > limit:
        problems.append(f"{len(nodes)} tasks is too many for this goal (max {limit}); merge steps")

    # 3. No repeated work.
    seen_desc: dict[str, str] = {}
    seen_act: dict[str, str] = {}
    for n in nodes:
        desc = _norm(n.description)
        if desc in seen_desc:
            problems.append(f"tasks {seen_desc[desc]!r} and {n.id!r} have the same description")
        seen_desc.setdefault(desc, n.id)
        if n.tool and n.arguments:  # no arguments = a role-level task, not a concrete action
            act = f"{n.tool}|{sorted(n.arguments.items())}"
            if act in seen_act:
                problems.append(f"tasks {seen_act[act]!r} and {n.id!r} run the same action")
            seen_act.setdefault(act, n.id)

    # 3b. One action per task: a command run and a file write cannot share a task.
    for n in nodes:
        if _RUN_AND_SAVE.search(n.description):
            problems.append(f"task {n.id!r} runs a command AND saves output; make them two tasks")

    # 3c. A read-only worker cannot produce command evidence, so a success check
    # that needs command output could never pass: the step stalled until the
    # mission was blocked (seen live: a "testing" task requiring stdout 'ALL_PASS').
    for n in nodes:
        if n.worker_role in (WorkerRole.RESEARCH, WorkerRole.TESTING):
            for t in sorted(set(_leaf_types(n.success_predicate)) & _NEEDS_COMMAND_RUN):
                problems.append(
                    f"task {n.id!r} ({n.worker_role.value}) cannot prove {t!r}: only a task with "
                    'worker_role "execute" runs commands; make it an execute task'
                )

    # 4. Ordering: code that builds on research must depend on it.
    research = {n.id for n in nodes if n.worker_role is WorkerRole.RESEARCH}
    if research:
        for n in nodes:
            if n.worker_role is WorkerRole.CODING and not (research & _ancestors(graph, n.id)):
                problems.append(f"coding task {n.id!r} must depend on a research task")
    return problems
