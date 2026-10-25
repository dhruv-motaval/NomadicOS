"""Phase 1 brain-integration tests (SPEC §19-21, §56.7-10).

The integrated path: NomadicBrain Actor<->Monitor proposes for the
current trusted plan step, the accepted candidate re-enters the SAME
untrusted parse/validate/authorize pipeline as raw model text, and only
the authoritative verifier may declare SUCCESS. These tests prove the
safety properties, not call ordering.

Since Phase 2 the proposal path runs the ChainedMonitor (structural +
semantic). The tests script BOTH cognitive roles: the actor prompt
carries ``goal_objective``, the monitor prompt contains
``CANDIDATES AND CONTEXT``.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_app, write_json

from nomadicos.brain.state import BrainState
from nomadicos.contracts.action import AuthorizedAction
from nomadicos.contracts.core import TaskStatus
from nomadicos.orchestration.brain_adapter import load_brain_state

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_exists", "path": "report.txt"}]


def candidate_json(
    *,
    cid: str = "c1",
    kind: str = "EXECUTE",
    tool: str = "filesystem.write",
    arguments: dict | None = None,
    effect: str = "report.txt exists with HELLO-GRAPH",
    rationale: str = "write the file directly",
    subgoal_id: str = "s0",
) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "id": cid,
                    "kind": kind,
                    "subgoal_id": subgoal_id,
                    "tool": tool,
                    "arguments": arguments or {"path": "report.txt", "content": "HELLO-GRAPH"},
                    "preconditions": [],
                    "expected_effect": effect,
                    "rationale": rationale,
                }
            ]
        }
    )


def verdict_json(cid: str, accepted: bool = True, reason: str = "aligned") -> str:
    return json.dumps({"verdicts": [{"candidate_id": cid, "accepted": accepted, "reason": reason}]})


def brain_scripts(
    actor_reply: str | list[str], *, monitor_id: str = "c1", monitor_accept: bool = True
) -> list[tuple[str, str | list[str]]]:
    """Actor + semantic-monitor scripts for the Phase-2 monitor chain.

    The actor prompt carries ``goal_objective``; the monitor prompt
    contains ``CANDIDATES AND CONTEXT``. MockEngine pops list responses in
    order, so a list actor reply drives multi-step missions.
    """
    verdicts = json.dumps(
        {
            "verdicts": [
                {
                    "candidate_id": monitor_id,
                    "accepted": monitor_accept,
                    "reason": "checked",
                }
            ]
        }
    )
    return [("goal_objective", actor_reply), ("CANDIDATES AND CONTEXT", verdicts)]


async def test_brain_candidate_reaches_validate_authorize_execute(tmp_path: Path) -> None:
    """A valid Brain candidate flows through validation, authorization and
    real execution; the mission is SUCCESS only via the goal verifier."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert summary.goal_verdict == "PASS"
    assert summary.executions == 1
    written = list((tmp_path / "ws").rglob("report.txt"))
    assert written and written[0].read_text(encoding="utf-8") == "HELLO-GRAPH"
    # the brain path was used (BRAIN_PROPOSED logged) and authorization happened
    events = [e.type for e in app.log.events(summary.task_id)]
    assert "BRAIN_PROPOSED" in events
    assert "AUTHORIZATION_GRANTED" in events
    # feedback: the observation reached the BrainState (bounded data only)
    values = app.graph.get_state(
        {"configurable": {"thread_id": f"nomadic:{summary.task_id}"}}
    ).values
    brain = load_brain_state(values.get("brain_state"))
    assert isinstance(brain, BrainState)
    assert brain.mission_id == summary.task_id
    assert len(brain.observations) == 1
    assert brain.action_candidates  # cognition data retained


async def test_malformed_brain_candidate_cannot_cause_side_effect(tmp_path: Path) -> None:
    """An authority-shaped candidate argument is rejected by the SAME
    parser as raw model output; no side effect occurs and the failure is
    typed and traceable."""
    poisoned = candidate_json(
        tool="filesystem.write",
        arguments={"path": "evil.txt", "content": "x", "authorized": True},
    )
    app = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(poisoned))
    summary = await app.run_goal(
        "write evil.txt", predicates=[{"type": "file_exists", "path": "evil.txt"}]
    )
    assert list((tmp_path / "ws").rglob("evil.txt")) == []
    assert summary.executions == 0
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)
    failures = app.log.events(summary.task_id)
    rejected = [e for e in failures if e.type.value == "BRAIN_PROPOSED" and e.result == "REJECTED"]
    assert rejected, [e.type.value for e in failures]
    assert any("authorized" in str(e.payload) for e in rejected)


async def test_unauthorized_brain_action_never_reaches_executor(tmp_path: Path) -> None:
    """Without autonomy grants the authorization service denies the brain
    proposal; the executor never runs the action."""
    app = make_app(
        tmp_path,
        grant=False,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    assert list((tmp_path / "ws").rglob("report.txt")) == []
    events = [e.type for e in app.log.events(summary.task_id)]
    assert "AUTHORIZATION_GRANTED" not in events
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)


async def test_executor_receives_authorized_action_not_raw_candidate(tmp_path: Path) -> None:
    """The executor is only ever handed an AuthorizedAction artifact; the
    Brain candidate object itself never reaches execution."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    original = app.executor.execute
    seen: list[object] = []

    async def spy(authorized, ctx):  # type: ignore[no-untyped-def]
        seen.append(authorized)
        return await original(authorized, ctx)

    app.executor.execute = spy  # type: ignore[method-assign]
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS
    assert seen and all(isinstance(a, AuthorizedAction) for a in seen)
    # artifact single-consumption: the issued table is empty after the run
    assert app.runtime.issued_count() == 0


async def test_step_results_feed_back_into_brain(tmp_path: Path) -> None:
    """A successful multi-step mission accumulates one Brain observation
    per executed step (bounded feedback, data only)."""
    actor_replies = [
        candidate_json(
            cid="c1",
            tool="filesystem.write",
            arguments={"path": "alpha.txt", "content": "A"},
            subgoal_id="s0",
        ),
        candidate_json(
            cid="c2",
            tool="filesystem.write",
            arguments={"path": "beta.txt", "content": "B"},
            subgoal_id="s1",
        ),
    ]
    monitor_verdicts = [
        json.dumps({"verdicts": [{"candidate_id": "c1", "accepted": True, "reason": "aligned"}]}),
        json.dumps({"verdicts": [{"candidate_id": "c2", "accepted": True, "reason": "aligned"}]}),
    ]
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=[
            ("goal_objective", actor_replies),
            ("CANDIDATES AND CONTEXT", monitor_verdicts),
        ],
    )
    summary = await app.run_goal(
        "Create alpha.txt and beta.txt",
        predicates=[
            {"type": "file_exists", "path": "alpha.txt"},
            {"type": "file_exists", "path": "beta.txt"},
        ],
    )
    assert summary.status is TaskStatus.SUCCESS and summary.executions == 2
    values = app.graph.get_state(
        {"configurable": {"thread_id": f"nomadic:{summary.task_id}"}}
    ).values
    brain = load_brain_state(values.get("brain_state"))
    assert isinstance(brain, BrainState)
    assert len(brain.observations) == 2


async def test_brain_claim_cannot_make_unverified_mission_success(tmp_path: Path) -> None:
    """A WAIT candidate becomes only a completion CLAIM; without evidence
    the authoritative verifier refuses SUCCESS."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(
            candidate_json(
                kind="WAIT",
                tool="",
                arguments={},
                effect="nothing to do",
                rationale="I think I am done",
            )
        ),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.goal_verdict != "PASS"
    assert summary.executions == 0


async def test_brain_failure_is_controlled_not_fallback(tmp_path: Path) -> None:
    """A Brain/model failure produces a typed traceable failure through
    bounded recovery - never a silent fallback to raw generation."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(RuntimeError("brain model exploded")),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)
    # no raw-generation fallback: the legacy prompt (REGISTERED ACTIONS) was
    # never sent; only brain-role prompts hit the engine
    for request in app.mock.calls:  # type: ignore[attr-defined]
        assert "REGISTERED ACTIONS" not in request.messages[-1].content
    failures = app.log.events(summary.task_id)
    assert any("brain" in str(e.result).lower() or e.type == "BRAIN_PROPOSED" for e in failures)


async def test_legacy_path_unchanged_when_brain_disabled(tmp_path: Path) -> None:
    """Compatibility: without the opt-in the existing raw-model workflow
    behaves exactly as before (no brain state in graph values)."""
    app = make_app(
        tmp_path,
        scripts=[("report.txt", write_json("report.txt", "HELLO-GRAPH"))],
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status is TaskStatus.SUCCESS
    values = app.graph.get_state(
        {"configurable": {"thread_id": f"nomadic:{summary.task_id}"}}
    ).values
    assert values.get("brain_state") is None
    assert "BRAIN_PROPOSED" not in [e.type for e in app.log.events(summary.task_id)]
