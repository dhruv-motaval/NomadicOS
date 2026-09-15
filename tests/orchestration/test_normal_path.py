"""Normal-path E2E against the graph with REAL filesystem tools (§7.26)."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.kernel.events import EventType

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_exists", "path": "report.txt"}]


async def test_full_normal_path_writes_real_file(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    # Phase 8: the goal's predicate (file_exists report.txt) is now INDEPENDENTLY
    # verified from real filesystem state, so SUCCESS is earned, claimed, or faked
    # (previous Phase-7 expectation was PARTIAL only because no verifier existed).
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert summary.goal_verdict == "PASS"
    assert summary.executions == 1 and summary.model_id == "models/small.gguf"
    written = list((tmp_path / "ws").rglob("report.txt"))
    assert written and written[0].read_text(encoding="utf-8") == "HELLO-GRAPH"
    notes = app.log.events(summary.task_id)
    order = [e.type for e in notes]
    for expected in (
        EventType.TASK_CREATED,
        EventType.TASK_STARTED,
        EventType.TASK_CLASSIFIED,
        EventType.TASK_PLANNED,
        EventType.MODEL_SELECTED,
        EventType.ACTION_PROPOSED,
        EventType.ACTION_VALIDATED,
        EventType.AUTHORIZATION_GRANTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_EXECUTED,
    ):
        assert expected in order, f"missing audit event {expected}"
    idx = {
        name: next(i for i, e in enumerate(order) if e is name)
        for name in (
            EventType.TASK_CREATED,
            EventType.MODEL_SELECTED,
            EventType.ACTION_VALIDATED,
            EventType.AUTHORIZATION_GRANTED,
            EventType.TOOL_EXECUTED,
        )
    }
    assert (
        idx[EventType.TASK_CREATED]
        < idx[EventType.MODEL_SELECTED]
        < idx[EventType.ACTION_VALIDATED]
        < idx[EventType.AUTHORIZATION_GRANTED]
        < idx[EventType.TOOL_EXECUTED]
    )


async def test_multi_step_plan_executes_each_step(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[
            ("alpha.txt", write_json("alpha.txt", "A")),
            ("beta.txt", write_json("beta.txt", "B")),
        ],
    )
    summary = await app.run_goal(
        "Perform each planned file creation step in order (one action per step).",
        predicates=[
            {"type": "file_exists", "path": "alpha.txt"},
            {"type": "file_exists", "path": "beta.txt"},
        ],
    )
    assert summary.executions == 2 and summary.status is TaskStatus.SUCCESS
    # both predicates independently verified:
    assert summary.goal_verdict == "PASS"
    created = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "ws").rglob("*.txt")}
    assert created == {"alpha.txt": "A", "beta.txt": "B"}


async def test_terminal_action_flows_through_graph(tmp_path: Path) -> None:
    import sys

    app = make_app(
        tmp_path,
        scripts=[
            (
                "version.txt",
                json.dumps(
                    {
                        "tool": "terminal",
                        "operation": "execute",
                        "args": {"command": sys.executable, "args": ["--version"]},
                    }
                ),
            )
        ],
    )
    summary = await app.run_goal(
        "Check the python version", predicates=[{"type": "artifact_created", "path": "version.txt"}]
    )
    needle_ok = any(
        e.type is EventType.TOOL_EXECUTED and e.result == "SUCCEEDED"
        for e in app.log.events(summary.task_id)
    )
    assert summary.executions == 1 and needle_ok


async def test_state_history_is_preserved(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HI"))],
    )
    summary = await app.run_goal(GOAL, predicates=PRED, task_id="task_hist")
    snapshot = app.graph.get_state({"configurable": {"thread_id": "nomadic:task_hist"}})
    values = snapshot.values
    assert str(values["goal"].objective) == GOAL
    assert len(values["proposals"]) == 1  # claim is separate, not a proposal
    assert len(values["executions"]) == 1
    assert len(values["observations"]) == 1
    assert values["executions"][0].model_id == "models/small.gguf"
    assert values["task_id"] == summary.task_id
    # proposal history survives recovery cycles too (append-only reducer)
    assert len(values["verifications"]) >= 2  # step + goal passes
    # single-use lifecycle at graph level: issued artifacts fully consumed
    assert values.get("pending_authorized_id") is None
    assert app.runtime.issued_count() == 0
