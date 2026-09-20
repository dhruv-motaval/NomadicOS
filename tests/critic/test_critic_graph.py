"""Worker/Critic loop on the real graph with scripted engines.

'Model' behavior (worker AND critic replies) is MANUALLY SUPPLIED here via
MockEngine scripts; live-model critic runs live in
tests/hardware/test_critic_live.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import nomadicos.orchestration.graph as graph_module
from nomadicos.agents.critic import ModelCritic
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.kernel.events import EventType
from nomadicos.orchestration.app import NomadicApp
from orchestration.helpers import make_app, write_json

GOAL_ANY = "Do the promised thing fully"
PRED_ANY = [
    {
        "any": [
            {"type": "file_exists", "path": "a.txt"},
            {"type": "file_exists", "path": "b.txt"},
        ]
    }
]
CRITIC_MODEL = "models/critic1.gguf"


def _improve_json() -> str:
    return json.dumps(
        {
            "decision": "IMPROVE",
            "score": 7.2,
            "critical_issues": [],
            "major_issues": ["neither a.txt nor b.txt was created; goal unmet"],
            "minor_issues": [],
            "suggestions": ["create b.txt"],
            "required_tests": [],
            "evidence_refs": ["exec:none"],
        }
    )


def wired_app(
    tmp_path: Path,
    critic_reply: str | list[str],
    *,
    critic_raises: bool = False,
    config_over: dict | None = None,
    worker_responses: list[str] | None = None,
):
    app = make_app(
        tmp_path,
        scripts=[
            ("Propose exactly one next action", worker_responses or ['{"finished": true}']),
            ("Evaluate now", critic_reply),
        ],
        config_over=config_over,
    )
    if critic_raises:
        from nomadicos.kernel.errors import ModelError

        app.mock.rules[-1] = ("Evaluate now", ModelError("critic dead"))
    app.registry.register(
        ModelRecord(
            model_id=CRITIC_MODEL,
            engine="mock",
            roles=["critic", "reasoning"],
            capabilities=[CapabilityTag.TEXT],
            health=ModelHealth.HEALTHY,
            source="manual",
            params={"size_hint_b": 26},
        )
    )
    app.runtime.critic = ModelCritic(app.registry, app.selector, app.runtime.engine_for)
    return app


def state_of(app: NomadicApp, task_id: str) -> dict:
    return app.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}}).values


async def test_critic_improve_then_worker_repair_then_verified_success(tmp_path: Path) -> None:
    app = wired_app(
        tmp_path,
        _improve_json(),
        worker_responses=[
            write_json("x.txt", "irrelevant"),  # step 0: creates neither a nor b
            write_json("y.txt", "irrelevant"),  # step 1: goal still unmet -> critique
            write_json("b.txt", "done"),  # after feedback: repair
        ],
    )
    summary = await app.run_goal(GOAL_ANY, predicates=PRED_ANY, task_id="loop1")
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    st = state_of(app, "loop1")
    iters = st["critic_iterations"]
    assert [i["decision"] for i in iters] == ["IMPROVE"]
    assert iters[0]["model_id"] == CRITIC_MODEL
    assert iters[0]["implementation_revision"]
    assert st["critic_feedback"]["for_revision"] == iters[0]["implementation_revision"]
    # feedback actually reached the next worker prompt:
    prompts = [c.messages[-1].content for c in app.mock.calls]
    assert any("CRITIC FEEDBACK" in p and "create b.txt" in p for p in prompts)
    assert any(
        e.type is EventType.CRITIC_REVIEWED and e.result == "IMPROVE"
        for e in app.log.events("loop1")
    )


async def test_claimed_accept_suppressed_and_loop_bounded(tmp_path: Path) -> None:
    accept = json.dumps({"decision": "ACCEPT", "score": 9.8})
    app = wired_app(
        tmp_path,
        accept,
        config_over={"budget": {"max_critic_iterations": 2}},
        worker_responses=[write_json(f"x{i}.txt", "nope") for i in range(3)]
        + ['{"finished": true}'],
    )
    summary = await app.run_goal(GOAL_ANY, predicates=PRED_ANY, task_id="loop2")
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}
    iters = state_of(app, "loop2")["critic_iterations"]
    assert iters and iters[0]["accept_suppressed"] is True
    assert iters[0]["decision"] == "IMPROVE"  # the stored system decision
    assert len(iters) <= 3  # bounded


async def test_critic_unavailable_never_fabrics_accept(tmp_path: Path) -> None:
    app = wired_app(
        tmp_path,
        '{"decision": "IMPROVE"}',  # overridden below by raises
        critic_raises=True,
        config_over={"budget": {"max_critic_iterations": 2, "max_recoveries": 2}},
        worker_responses=[write_json("q.txt", "x"), '{"finished": true}'],
    )
    summary = await app.run_goal(GOAL_ANY, predicates=PRED_ANY, task_id="loop3")
    assert summary.status is not TaskStatus.SUCCESS
    iters = state_of(app, "loop3")["critic_iterations"]
    assert iters and all(i["decision"] == "NOT_EVALUATED" for i in iters)
    assert state_of(app, "loop3").get("critic_feedback") is None


async def test_critic_reject_blocks_without_further_worker_turns(tmp_path: Path) -> None:
    reject = json.dumps(
        {"decision": "REJECT", "score": 3.0, "critical_issues": ["SQL injection sink"]}
    )
    app = wired_app(tmp_path, reject, worker_responses=[write_json("w.txt", "x")])
    summary = await app.run_goal(GOAL_ANY, predicates=PRED_ANY, task_id="loop4")
    assert summary.status is TaskStatus.BLOCKED
    worker_calls = [
        c for c in app.mock.calls if "Propose exactly one next action" in c.messages[-1].content
    ]
    # two plan steps ran BEFORE the critique; REJECT stops any further worker turn
    assert len(worker_calls) == 2


async def test_no_progress_stuck_detected_and_bounded(tmp_path: Path) -> None:
    app = wired_app(  # worker only claims; no executions -> identical revisions
        tmp_path, _improve_json(), worker_responses=['{"finished": true}']
    )
    summary = await app.run_goal(GOAL_ANY, predicates=PRED_ANY, task_id="loop5")
    assert summary.status is TaskStatus.BLOCKED
    iters = state_of(app, "loop5")["critic_iterations"]
    assert len(iters) == 2
    assert (
        "stuck"
        in str(iters[-1].get("decision"))
        + str(state_of(app, "loop5").get("outcome_note", "")).lower()
    )


def test_critic_nodes_cannot_write_success() -> None:
    import inspect

    src = inspect.getsource(graph_module)
    assert src.count('"task_status": TaskStatus.SUCCESS') == 1
    critique_src = src.split("async def critique")[1].split("# ------")[0]
    assert "TaskStatus.SUCCESS" not in critique_src
