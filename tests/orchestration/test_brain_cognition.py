"""The full brain on real missions: adaptive pass, deliberation, role
routing, belief updates, prediction error and trace capture.

Deliberation must CHANGE the outcome here: the first accepted candidate
writes the wrong content, so only the deliberated selection can pass the
goal verifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from helpers import make_app

from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.schemas import Prediction, WorldFact, WorldFactKind, WorldState
from nomadicos.contracts.core import Goal, PlanStep, TaskStatus
from nomadicos.contracts.execution import Observation, ObservationKind
from nomadicos.orchestration.brain_adapter import (
    MAX_PERSISTED_HISTORY,
    brain_state_for,
    dump_brain_state,
    load_brain_state,
    observe_into_brain,
)

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_contains", "path": "report.txt", "text": "HELLO-GRAPH"}]
COGNITION = {
    "orchestration": {
        "brain_adaptive": True,
        "brain_deliberation": True,
        "brain_search_depth": 0,
    }
}


def _candidate(cid: str, content: str) -> dict[str, Any]:
    return {
        "id": cid,
        "kind": "EXECUTE",
        "subgoal_id": "s0",
        "tool": "filesystem.write",
        "arguments": {"path": "report.txt", "content": content},
        "preconditions": [],
        "expected_effect": f"report.txt contains {content}",
        "rationale": "write the file",
    }


def _scripts(evaluator: str | list[str]) -> list[tuple[str, str | list[str]]]:
    prediction = json.dumps(
        {
            "predicted_world": {
                "version": 0,
                "facts": {"status": {"key": "status", "value": "SUCCEEDED", "kind": "PREDICTION"}},
            },
            "confidence": 0.8,
            "assumptions": [],
            "uncertainties": [],
        }
    )
    verdicts = json.dumps(
        {
            "verdicts": [
                {"candidate_id": "c1", "accepted": True, "reason": "ok"},
                {"candidate_id": "c2", "accepted": True, "reason": "ok"},
            ]
        }
    )
    actor = json.dumps({"candidates": [_candidate("c1", "WRONG"), _candidate("c2", "HELLO-GRAPH")]})
    # first match wins and every payload carries goal_objective: actor last
    return [
        ("STATE AND CANDIDATE", prediction),
        ("GOAL, CANDIDATE AND PREDICTION", evaluator),
        ("CANDIDATES AND CONTEXT", verdicts),
        ("goal_objective", actor),
    ]


def _evaluation(score: float) -> str:
    factors = dict.fromkeys(
        ("goal_progress", "feasibility", "risk", "uncertainty", "resource_cost", "reversibility"),
        0.5,
    )
    return json.dumps({"score": score, "factors": factors, "rationale": "estimate"})


async def test_deliberation_selects_the_better_candidate_and_everything_is_traced(
    tmp_path: Path,
) -> None:
    app = make_app(
        tmp_path,
        brain_actor=True,
        config_over=COGNITION,
        scripts=_scripts([_evaluation(0.2), _evaluation(0.9)]),  # c1 then c2
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    written = list((tmp_path / "ws").rglob("report.txt"))
    assert written[0].read_text(encoding="utf-8") == "HELLO-GRAPH"  # c2, not the first (c1)

    trace = app.brain_trace(summary.task_id)
    cycle = next(c for c in trace.cycles if c.selected_action)
    assert cycle.selected_action["candidate_id"] == "c2"
    assert {p["candidate_id"] for p in cycle.predictions} == {"c1", "c2"}
    assert set(cycle.adaptive) >= {
        "BRAIN_ATTENTION_FOCUSED",
        "BRAIN_BELIEFS_ASSESSED",
        "BRAIN_METACOGNITION_PRODUCED",
        "BRAIN_EFFORT_DECIDED",
    }
    [error] = cycle.prediction_errors
    assert (error["candidate_id"], error["outcome"], error["matched"]) == ("c2", 1.0, ["status"])
    assert error["calibration_error"] == pytest.approx(0.2)
    # beliefs: the real observation became observed (not verified) world facts
    values = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{summary.task_id}"}})
    brain = load_brain_state(values.values["brain_state"])
    assert brain is not None
    assert brain.world.facts["status"].value == "SUCCEEDED"
    assert brain.world.facts["status"].kind is WorldFactKind.FACT


async def test_failed_deliberation_keeps_the_monitor_accepted_candidate(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        brain_actor=True,
        config_over=COGNITION,
        scripts=_scripts("not json"),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    # c1 (first accepted) ran: the mission does not pass, but it was not
    # blocked by deliberation and nothing ran without monitor acceptance
    written = list((tmp_path / "ws").rglob("report.txt"))
    assert written and written[0].read_text(encoding="utf-8") in {"WRONG", "HELLO-GRAPH"}
    assert "BRAIN_PROPOSED" in [e.type for e in app.log.events(summary.task_id)]
    trace = app.brain_trace(summary.task_id)
    assert not any(c.selected_action for c in trace.cycles)
    # deliberation chose nothing, so no prediction may be scored: an older
    # prediction for a reused id like "c1" is not about this action
    assert not any(c.prediction_errors for c in trace.cycles)


async def test_role_routing_runs_the_mission(tmp_path: Path) -> None:
    over = {"orchestration": {**COGNITION["orchestration"], "brain_role_routing": True}}
    app = make_app(
        tmp_path,
        brain_actor=True,
        config_over=over,
        scripts=_scripts([_evaluation(0.2), _evaluation(0.9)]),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note


def test_trace_of_a_task_without_brain_events_is_refused(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    with pytest.raises(ValueError, match="no captured brain events"):
        app.brain_trace("task_never_ran")


def _brain():  # type: ignore[no-untyped-def]
    goal = Goal(objective="write a file")
    return brain_state_for(goal, [PlanStep(id="p0", description="write")], task_id="t1")


def _prediction(confidence: float, status: str) -> Prediction:
    return Prediction(
        id="pred1",
        candidate_id="c1",
        predicted_world=WorldState(
            facts={
                "status": WorldFact(key="status", value=status, kind=WorldFactKind.PREDICTION),
                "exit_code": WorldFact(key="exit_code", value=0, kind=WorldFactKind.PREDICTION),
            }
        ),
        confidence=confidence,
    )


def _obs(**data: Any) -> Observation:
    return Observation(task_id="t1", kind=ObservationKind.STEP_RESULT, summary="ran", data=data)


def test_prediction_error_scores_a_wrong_prediction() -> None:
    brain = _brain().model_copy(update={"predictions": [_prediction(0.9, "SUCCEEDED")]})
    brain, events = observe_into_brain(
        brain, _obs(status="FAILED", exit_code=1), selected_prediction="pred1"
    )
    [error] = brain.prediction_errors
    assert (error.outcome, error.calibration_error) == (0.0, 0.9)
    assert sorted(error.mismatched) == ["exit_code", "status"] and error.matched == []
    assert [e.event_type for e in events] == [BrainEventType.BRAIN_PREDICTION_ERROR_MEASURED]
    assert brain.history[-1].event_id == events[0].event_id


def test_prediction_error_needs_a_selection_a_prediction_and_an_execution() -> None:
    with_pred = _brain().model_copy(update={"predictions": [_prediction(0.5, "SUCCEEDED")]})
    for brain, obs, selected in (
        (with_pred, _obs(status="SUCCEEDED"), None),  # not chosen by deliberation
        (_brain(), _obs(status="SUCCEEDED"), "pred1"),  # that prediction is gone
        (with_pred, _obs(note="no status"), "pred1"),  # not an execution result
    ):
        after, events = observe_into_brain(brain, obs, selected_prediction=selected)
        assert after.prediction_errors == [] and events == []
        assert len(after.observations) == len(brain.observations) + 1  # still fed


def test_long_missions_persist_a_mirror_that_still_loads() -> None:
    brain = _brain()
    for i in range(MAX_PERSISTED_HISTORY + 40):
        brain = brain.with_event(
            BrainEvent(
                event_id=f"e{i}",
                mission_id="t1",
                event_type=BrainEventType.BRAIN_EFFORT_DECIDED,
                actor="effort",
            )
        )
    prediction = _prediction(0.5, "SUCCEEDED")
    brain = brain.model_copy(update={"predictions": [prediction] * 80})  # over its cap of 64
    loaded = load_brain_state(dump_brain_state(brain))
    assert loaded is not None
    assert len(loaded.history) == MAX_PERSISTED_HISTORY
    assert loaded.history[-1].event_id == f"e{MAX_PERSISTED_HISTORY + 39}"  # newest kept
    assert len(loaded.predictions) == 64
