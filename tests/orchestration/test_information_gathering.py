"""Information gathering on real missions: the plan declares the facts it
does not know, the brain plans a real read for them (not the tool-less
research worker), and the read turns each into an observed fact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import make_app

from nomadicos.brain.decomposer import apply_decomposition, parse_decomposition
from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.schemas import ActionCandidate, CandidateKind, WorldFactKind
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.contracts.execution import Observation, ObservationKind
from nomadicos.orchestration.brain_adapter import (
    load_brain_state,
    needs_answered_by,
    observe_into_brain,
    open_unknowns,
)
from nomadicos.orchestration.page_text import best_window, readable_text

GOAL = "Look up the Fable 5.2 input price and write it to report.txt"
UNKNOWN = {"key": "fable_price", "question": "What is the Fable 5.2 input price?"}


def _plan(**extra: Any) -> str:
    return json.dumps(
        {
            "tasks": [
                {"id": "t0", "description": "look up the price", "worker_role": "research"},
                {
                    "id": "t1",
                    "description": "write the price to report.txt",
                    "depends_on": ["t0"],
                },
            ],
            **extra,
        }
    )


def _candidate(subgoal: str, kind: str, tool: str, args: dict[str, str], effect: str) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "id": "c1",
                    "kind": kind,
                    "subgoal_id": subgoal,
                    "tool": tool,
                    "arguments": args,
                    "preconditions": [],
                    "expected_effect": effect,
                    "rationale": "",
                }
            ]
        }
    )


async def test_unknown_fact_is_read_for_real_then_used(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "pricing.txt").write_text("Fable 5.2 input: PRICE-42", encoding="utf-8")
    read = _candidate(
        "s0", "OBSERVE", "filesystem.read", {"path": "pricing.txt"}, "reads fable_price"
    )
    write = _candidate(
        "s1", "EXECUTE", "filesystem.write", {"path": "report.txt", "content": "PRICE-42"}, "done"
    )
    verdicts = json.dumps({"verdicts": [{"candidate_id": "c1", "accepted": True, "reason": "ok"}]})
    app = make_app(
        tmp_path,
        brain_planner=True,
        brain_actor=True,
        workspace_per_task=False,
        config_over={"orchestration": {"brain_adaptive": True}},
        # decomposer first, then the actor per task (all share goal_objective)
        scripts=[
            ("CANDIDATES AND CONTEXT", verdicts),
            ("goal_objective", [_plan(unknowns=[UNKNOWN]), read, write]),
        ],
    )
    summary = await app.run_goal(
        GOAL, predicates=[{"type": "file_contains", "path": "report.txt", "text": "PRICE-42"}]
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note

    # the research task went to the actor (it read the file), not the worker
    actor_prompts = [
        c.messages[-1].content for c in app.mock.calls if "information_needs" in str(c.messages)
    ]
    assert actor_prompts and "What is the Fable 5.2 input price?" in actor_prompts[0]
    values = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{summary.task_id}"}})
    brain = load_brain_state(values.values["brain_state"])
    assert brain is not None
    fact = brain.world.facts["fable_price"]
    assert fact.kind is WorldFactKind.FACT  # observed, never VERIFIED
    assert "PRICE-42" in str(fact.value)
    assert open_unknowns(brain) == []
    events = app.brain_trace(summary.task_id).events
    assert "BRAIN_INFO_GATHERING_REQUESTED" in {e.event_type.value for e in events}


#: a page whose answer sits far past any 500-char cut of the raw markup
PAGE = (
    "<!DOCTYPE html><html><head><title>Pricing</title>"
    + "<script>var x = 'noise';</script>" * 60
    + "<style>.a{color:red}</style></head><body><nav>Home | Docs</nav>"
    + "<p>Lorem ipsum filler text.</p>" * 80
    + "<table><tr><td>Fable 5.2</td><td>input price PRICE-42 per MTok</td></tr></table>"
    + "<p>Footer filler.</p>" * 80
    + "</body></html>"
)


async def test_fetched_html_page_resolves_to_its_visible_answer(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "pricing.html").write_text(PAGE, encoding="utf-8")
    read = _candidate(
        "s0", "OBSERVE", "filesystem.read", {"path": "pricing.html"}, "reads fable_price"
    )
    write = _candidate(
        "s1", "EXECUTE", "filesystem.write", {"path": "report.txt", "content": "PRICE-42"}, "done"
    )
    verdicts = json.dumps({"verdicts": [{"candidate_id": "c1", "accepted": True, "reason": "ok"}]})
    app = make_app(
        tmp_path,
        brain_planner=True,
        brain_actor=True,
        workspace_per_task=False,
        config_over={"orchestration": {"brain_adaptive": True}},
        scripts=[
            ("CANDIDATES AND CONTEXT", verdicts),
            ("goal_objective", [_plan(unknowns=[UNKNOWN]), read, write]),
        ],
    )
    summary = await app.run_goal(
        GOAL, predicates=[{"type": "file_contains", "path": "report.txt", "text": "PRICE-42"}]
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    values = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{summary.task_id}"}})
    brain = load_brain_state(values.values["brain_state"])
    assert brain is not None
    value = str(brain.world.facts["fable_price"].value)
    assert "PRICE-42" in value
    assert "<" not in value and "noise" not in value  # visible text, not markup


def test_readable_text_keeps_visible_text_only() -> None:
    text = readable_text(PAGE)
    assert "Pricing" in text and "input price PRICE-42 per MTok" in text
    assert "noise" not in text and "color:red" not in text and "<" not in text
    assert readable_text("plain stdout <3") == "plain stdout <3"  # not a page


def test_best_window_finds_the_part_that_answers() -> None:
    text = "filler " * 500 + "Fable 5.2 input price is PRICE-42" + " filler" * 500
    window = best_window(text, UNKNOWN["question"], 200)
    assert len(window) == 200 and "PRICE-42" in window
    assert best_window("short", "anything", 200) == "short"


def test_plan_unknowns_become_unknown_facts() -> None:
    goal = Goal(objective=GOAL)
    decomposition = parse_decomposition(_plan(unknowns=[UNKNOWN, UNKNOWN]), goal_id=goal.id)
    brain = apply_decomposition(BrainState.for_goal(goal), decomposition)
    assert open_unknowns(brain) == ["fable_price"]  # duplicates collapse
    assert brain.world.facts["fable_price"].value == UNKNOWN["question"]


@pytest.mark.parametrize(
    "bad",
    [
        {"key": "has space", "question": "q"},
        {"key": "k", "question": " "},
        {"key": "k", "question": "q", "value": "guessed"},  # closed schema
    ],
)
def test_malformed_unknowns_fail_closed(bad: dict[str, str]) -> None:
    with pytest.raises(ModelOutputRejected):
        parse_decomposition(_plan(unknowns=[bad]), goal_id="goal_1")


def _brain_with_unknown() -> BrainState:
    goal = Goal(objective=GOAL)
    return apply_decomposition(
        BrainState.for_goal(goal, mission_id="t1"),
        parse_decomposition(_plan(unknowns=[UNKNOWN]), goal_id=goal.id),
    )


def _obs(status: str) -> Observation:
    return Observation(
        task_id="t1",
        kind=ObservationKind.STEP_RESULT,
        summary="read",
        data={"status": status, "stdout_head": "PRICE-42"},
    )


def test_only_a_successful_read_resolves_and_only_what_it_names() -> None:
    brain = _brain_with_unknown()
    reads = ActionCandidate(
        id="c1", kind=CandidateKind.OBSERVE, tool="filesystem.read", expected_effect="fable_price"
    )
    other = ActionCandidate(id="c2", kind=CandidateKind.OBSERVE, tool="filesystem.read")
    assert needs_answered_by(brain, reads) == ["fable_price"]
    assert needs_answered_by(brain, other) == []
    failed, _ = observe_into_brain(brain, _obs("FAILED"), answers=["fable_price"])
    assert open_unknowns(failed) == ["fable_price"]
    done, _ = observe_into_brain(brain, _obs("SUCCEEDED"), answers=["fable_price"])
    assert open_unknowns(done) == []
    assert done.world.facts["fable_price"].value == "PRICE-42"


# --- regression: an action goal on a local app must not turn into a web fetch
# (live run task_a9f5cf7fc0ae48498946: the actor invented "Notepad download URL
# is unknown", the semantic monitor accepted it, and a curl ran instead of a launch)

APP_GOAL = "Open Notepad and type the line: Hello from NomadicOS"
APP_PRED = {"type": "window_present", "title": "Notepad"}


def _app_candidates() -> str:
    fetch = {
        "id": "c1",
        "kind": "OBSERVE",
        "subgoal_id": "s0",
        "tool": "terminal.execute",
        "arguments": {
            "command": "curl",
            "args": ["-sL", "https://notepad-plus-plus.org/downloads/"],
        },
        "preconditions": [],
        "expected_effect": "learn the Notepad download URL",
        "rationale": "Notepad download URL is unknown",
    }
    launch = {
        "id": "c2",
        "kind": "EXECUTE",
        "subgoal_id": "s0",
        "tool": "process.launch",
        "arguments": {"executable": "notepad.exe"},
        "preconditions": [],
        "expected_effect": "a Notepad window is open",
        "rationale": "open the app",
    }
    return json.dumps({"candidates": [fetch, launch]})


async def _app_step(brain: BrainState) -> Any:
    from nomadicos.inference.mock import MockEngine
    from nomadicos.orchestration.brain_adapter import brain_step

    accept_all = json.dumps(
        {
            "verdicts": [
                {"candidate_id": "c1", "accepted": True, "reason": "Notepad URL is unknown"},
                {"candidate_id": "c2", "accepted": True, "reason": "ok"},
            ]
        }
    )
    engine = MockEngine()
    engine.script("CANDIDATES AND CONTEXT", accept_all)
    engine.script("goal_objective", _app_candidates())
    subgoal = brain.task_graph.subgoals[0]
    return await brain_step(brain, subgoal, engine=engine, model_id="mock", adaptive=True)


async def test_local_app_goal_never_selects_a_web_fetch() -> None:
    from nomadicos.contracts.core import PlanStep
    from nomadicos.orchestration.brain_adapter import brain_state_for

    goal = Goal.from_spec(APP_GOAL, predicates=[APP_PRED])
    brain = brain_state_for(
        goal, [PlanStep(description="Satisfy window_present for Notepad")], task_id="t_app"
    )
    result = await _app_step(brain)
    assert result.candidate is not None and result.candidate.id == "c2"  # the launch, not curl
    verdicts = {r.candidate_id: r for r in result.brain.monitor_results}
    assert verdicts["c1"].decision.value == "REJECTED"
    assert "STRUCTURAL INFORMATION" in verdicts["c1"].reasons[0]


async def test_web_read_stays_allowed_for_an_open_information_need() -> None:
    from nomadicos.brain.schemas import WorldFact, WorldState
    from nomadicos.contracts.core import PlanStep
    from nomadicos.orchestration.brain_adapter import brain_state_for

    goal = Goal.from_spec(APP_GOAL, predicates=[APP_PRED])
    brain = brain_state_for(
        goal, [PlanStep(description="Satisfy window_present for Notepad")], task_id="t_app"
    )
    need = WorldFact(key="np_url", value="Where is Notepad?", kind=WorldFactKind.UNKNOWN)
    brain = brain.model_copy(update={"world": WorldState(version=1, facts={"np_url": need})})
    result = await _app_step(brain)
    assert result.candidate is not None and result.candidate.id == "c1"  # gate is need-scoped
