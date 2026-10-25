"""Sprint 6 — attention: bounded, deterministic context selection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.brain.attention import compute_attention
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.schemas import (
    MAX_ATTENTION_ITEMS,
    ActionCandidate,
    AttentionItem,
    AttentionSource,
    AttentionView,
    CandidateKind,
    MemoryRef,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.execution import Observation, ObservationKind


def focused_state() -> BrainState:
    goal = Goal(
        id="task_g",
        objective="fix the auth module",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )
    sub = Subgoal(id="subgoal_s1", goal_id="task_g", description="write failing auth test")
    graph = TaskGraph.build(goal_id="task_g", subgoals=[sub])
    return BrainState(
        goal=goal,
        task_graph=graph,
        active_subgoal=sub,
        world=WorldState(
            facts={
                "auth_status": WorldFact(key="auth_status", value="ok", kind=WorldFactKind.FACT),
                "db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN),
            }
        ),
        observations=[
            Observation(
                id="obs_1",
                task_id="task_g",
                kind=ObservationKind.STEP_RESULT,
                summary="auth module test failed",
            ),
            Observation(
                id="obs_2",
                task_id="task_g",
                kind=ObservationKind.NOTE,
                summary="unrelated weather note",
            ),
        ],
        relevant_memories=[MemoryRef(memory_id="mem_auth", score=0.9)],
        action_candidates=[
            ActionCandidate(
                id="cand_a",
                kind=CandidateKind.EXECUTE,
                tool="terminal",
                subgoal_id="subgoal_s1",
            )
        ],
    )


def test_active_subgoal_outranks_everything() -> None:
    view = compute_attention(focused_state())
    assert view.items[0].source is AttentionSource.SUBGOAL
    assert view.items[0].ref == "subgoal_s1"
    assert view.items[0].score >= max(i.score for i in view.items[1:])


def test_unknown_fact_outranks_verified_fact() -> None:
    state = focused_state().model_copy(
        update={
            "world": WorldState(
                facts={
                    "verified": WorldFact(
                        key="verified", value="x", kind=WorldFactKind.VERIFIED_FACT
                    ),
                    "mystery": WorldFact(key="mystery", kind=WorldFactKind.UNKNOWN),
                }
            )
        }
    )
    by_ref = {i.ref: i for i in compute_attention(state).items}
    assert by_ref["mystery"].score > by_ref["verified"].score  # uncertainty ranks up


def test_goal_relevance_boosts_matching_items() -> None:
    view = compute_attention(focused_state())
    obs = {i.ref: i.score for i in view.items if i.source is AttentionSource.OBSERVATION}
    assert obs["obs_1"] > obs["obs_2"]  # auth-related > unrelated


def test_memory_scores_feed_attention() -> None:
    state = focused_state().model_copy(
        update={
            "relevant_memories": [
                MemoryRef(memory_id="mem_strong", score=1.0),
                MemoryRef(memory_id="mem_weak", score=0.2),
            ]
        }
    )
    view = compute_attention(state)
    scores = {i.ref: i.score for i in view.items if i.source is AttentionSource.MEMORY}
    assert scores["mem_strong"] > scores["mem_weak"]  # Sprint 5 retrieval score drives salience


def test_candidate_dependency_boosts_active_subgoal() -> None:
    # the candidate targeting the active subgoal outranks one that does not
    state = focused_state().model_copy(
        update={
            "action_candidates": [
                ActionCandidate(
                    id="cand_hit",
                    kind=CandidateKind.EXECUTE,
                    tool="auth tool",
                    subgoal_id="subgoal_s1",
                ),
                ActionCandidate(
                    id="cand_miss",
                    kind=CandidateKind.EXECUTE,
                    tool="auth tool",
                    subgoal_id="subgoal_other",
                ),
            ]
        }
    )
    view = compute_attention(state)
    scores = {i.ref: i.score for i in view.items if i.source is AttentionSource.ACTION_CANDIDATE}
    assert scores["cand_hit"] > scores["cand_miss"]


def test_attention_view_is_bounded() -> None:
    state = focused_state()
    for i in range(30):
        state = state.model_copy(
            update={
                "observations": [
                    *state.observations,
                    Observation(
                        id=f"obs_fill_{i}",
                        task_id="task_g",
                        kind=ObservationKind.NOTE,
                        summary=f"note {i} auth",
                    ),
                ]
            }
        )
    view = compute_attention(state)
    assert len(view.items) <= MAX_ATTENTION_ITEMS  # hard bound, never expanded


def test_attention_is_deterministic() -> None:
    state = focused_state()
    assert compute_attention(state) == compute_attention(state)
    # identical inputs construct identical views; ordering is (-score, ref)
    view = compute_attention(state)
    refs = [i.ref for i in view.items]
    assert refs == [i.ref for i in sorted(view.items, key=lambda x: (-x.score, x.ref))]


def test_attention_never_mutates_state() -> None:
    state = focused_state()
    before = state.model_dump(mode="json")
    compute_attention(state)
    assert state.model_dump(mode="json") == before


def test_attention_view_rejects_unordered_or_unbounded_items() -> None:
    a = AttentionItem(ref="a", source=AttentionSource.SUBGOAL, score=0.9)
    b = AttentionItem(ref="b", source=AttentionSource.SUBGOAL, score=0.5)
    with pytest.raises(ValidationError):
        AttentionView(items=[b, a])  # out of deterministic order
    with pytest.raises(ValidationError):
        AttentionView(
            items=[
                AttentionItem(ref=f"r{i}", source=AttentionSource.MEMORY, score=0.5)
                for i in range(MAX_ATTENTION_ITEMS + 1)
            ]
        )


def test_attention_item_is_data_only() -> None:
    # closed schema: authority-shaped extras are structurally impossible
    with pytest.raises(ValidationError):
        AttentionItem(ref="a", source=AttentionSource.SUBGOAL, score=0.9, authorized=True)  # type: ignore[call-overload]
