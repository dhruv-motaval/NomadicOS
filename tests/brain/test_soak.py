"""Sprint 10 — soak harness: bounded, deterministic, production-hardening
invariants over repeated representative Brain workflows."""

from __future__ import annotations

import tempfile
from datetime import datetime
from itertools import count
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.brain.adaptive import AdaptiveController
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.soak import MAX_SOAK_ITERATIONS, run_soak
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal
from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryRecord
from nomadicos.contracts.skills import SkillDefinition, SkillParameter, SkillQuery
from nomadicos.memory.semantic import record_fact
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.memory.supersession import supersede
from nomadicos.skills.registry import SkillRegistry
from nomadicos.skills.retrieval import rank_skills


def factory() -> BrainState:
    goal = Goal(id="task_g", objective="fix the auth module", predicates=[])
    return BrainState.for_goal(goal, task_graph=TaskGraph.build(goal_id="task_g"))


def adaptive_workflow() -> object:
    """Representative workflow: adaptive one-pass with INJECTED clock/ids
    so every pass is replayable (ids derive from iteration + pass index)."""

    def workflow(state: BrainState, iteration: int) -> BrainState:
        seq = count(1)  # fresh per pass: ids depend only on (iteration, index)
        controller = AdaptiveController(
            now=lambda: datetime(2026, 1, 1),
            event_id=lambda: f"evt_{iteration:04d}_{next(seq):06d}",
        )
        return controller.run(state)

    return workflow


def test_soak_repeated_execution_green_and_deterministic() -> None:
    report = run_soak(adaptive_workflow(), factory, iterations=12)
    assert report.passed == 12 and report.failed == 0
    assert report.duplicate_events == 0  # no cross-iteration duplicate events
    assert report.deterministic is True  # replay reproduces every fingerprint
    # every iteration starts from the factory: one pass = 4 stage events +
    # the final transition → version 6 (strict growth within each pass)
    assert report.final_version == 6


def test_soak_detects_state_corruption() -> None:
    def stalling(state: BrainState, iteration: int) -> BrainState:
        # corrupted transition: version does not strictly increase
        return state.model_copy(update={"state_version": state.state_version})

    report = run_soak(stalling, factory, iterations=3)
    assert report.failed == 3
    assert any("strictly increase" in f for f in report.failures)


def test_soak_detects_mutation() -> None:
    report = run_soak(lambda state, iteration: state, factory, iterations=2)
    assert report.failed == 2  # returning the SAME object is a violation


def test_soak_is_bounded() -> None:
    report = run_soak(adaptive_workflow(), factory, iterations=MAX_SOAK_ITERATIONS + 25)
    assert report.iterations == MAX_SOAK_ITERATIONS  # hard bound
    from nomadicos.brain.soak import SoakReport

    with pytest.raises(ValidationError):
        SoakReport(iterations=3, passed=1, failed=1)  # counts must sum


def test_soak_records_failures_bounded() -> None:
    def exploding(state: BrainState, iteration: int) -> BrainState:
        raise RuntimeError("boom")

    report = run_soak(exploding, factory, iterations=4)
    assert report.passed == 0 and report.failed == 4
    assert len(report.failures) <= 8  # bounded failure list
    assert any("RuntimeError" in f for f in report.failures)


def test_soak_detects_duplicate_events_across_iterations() -> None:
    def stale_workflow(state: BrainState, iteration: int) -> BrainState:
        seq = count(1)  # counter RESETS per iteration: same ids every pass
        controller = AdaptiveController(
            now=lambda: datetime(2026, 1, 1),
            event_id=lambda: f"evt_{next(seq):06d}",
        )
        return controller.run(state)

    report = run_soak(stale_workflow, factory, iterations=4)
    assert report.duplicate_events > 0  # stale event ids counted, not silent


def test_soak_full_production_workflow_memory_skills() -> None:
    """Representative production pass: adaptive cognition + memory write +
    supersession + skill retrieval, repeated (bounded)."""

    def workflow(state: BrainState, iteration: int) -> BrainState:
        seq = count(1)  # fresh per pass: replayable ids
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlMemoryStore(Path(tmp) / "m.jsonl")
            fact = record_fact(store, task_id=f"task_{iteration}", fact=f"fact {iteration}")
            if fact is not None and iteration > 0:
                replacement = MemoryRecord(
                    id=f"mem_new_{iteration}",
                    kind=MemoryKind.SEMANTIC,
                    content=f"replacement {iteration}",
                    provenance=MemoryProvenance(confidence=0.9, verified=True),
                )
                supersede(store, old_id=fact.id, new=replacement)
            registry = SkillRegistry()
            registry.register(
                SkillDefinition(
                    skill_id="probe",
                    name="probe",
                    description="probes the workspace",
                    capability="filesystem.read",
                    inputs=[SkillParameter(name="path", type="string")],
                )
            )
            rank_skills(
                registry,
                SkillQuery(capability="filesystem.read", provided_args=["path"]),
            )
        controller = AdaptiveController(
            now=lambda: datetime(2026, 1, 1),
            event_id=lambda: f"evt_{iteration:04d}_{next(seq):06d}",
        )
        return controller.run(state)

    report = run_soak(workflow, factory, iterations=6)
    assert report.passed == 6 and report.failed == 0
    assert report.duplicate_events == 0
    assert report.deterministic is True
