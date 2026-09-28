"""Canonical brain contracts: required fields, invalid values, stable ids,
serialization, reuse, and the brain package import boundary (Sprint 1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.brain import contracts as brain_contracts
from nomadicos.brain.schemas import (
    BRAIN_SCHEMA_VERSION,
    ActionCandidate,
    BudgetState,
    CandidateKind,
    Capability,
    Constraints,
    Decision,
    Hypothesis,
    MemoryRef,
    RecoveryPlan,
    Subgoal,
    WorkingMemoryView,
    WorldFact,
    WorldFactKind,
    WorldState,
    canonical_json,
)
from nomadicos.contracts import (
    EvidenceItem,
    FailureRecord,
    Goal,
    Observation,
    VerificationResult,
)
from nomadicos.kernel.ids import new_id


def test_reused_nomadicos_types_are_reexports_not_duplicates() -> None:
    """Goal/Observation/Evidence/VerificationResult/FailureRecord come from
    the existing contracts package — the brain re-exports, never redefines."""
    assert brain_contracts.Goal is Goal
    assert brain_contracts.Observation is Observation
    assert brain_contracts.EvidenceItem is EvidenceItem
    assert brain_contracts.VerificationResult is VerificationResult
    assert brain_contracts.FailureRecord is FailureRecord


def test_schema_version_constant() -> None:
    assert BRAIN_SCHEMA_VERSION == 1
    assert brain_contracts.BRAIN_STATE_SCHEMA_VERSION == BRAIN_SCHEMA_VERSION
    assert brain_contracts.BRAIN_EVENT_SCHEMA_VERSION == BRAIN_SCHEMA_VERSION


def test_subgoal_validation() -> None:
    sub = Subgoal(id="subgoal_sg", goal_id="task_g", description="Prepare environment")
    assert sub.id == "subgoal_sg"
    with pytest.raises(ValidationError, match="goal_id"):
        Subgoal(goal_id="  ", description="x")
    with pytest.raises(ValidationError, match="description"):
        Subgoal(goal_id="task_g", description="  ")


def test_hypothesis_requires_explicit_confidence_and_statement() -> None:
    hyp = Hypothesis(
        id="hyp_h1",
        statement="config error caused the failure",
        confidence=0.55,
        supporting_evidence=["obs_1"],
    )
    assert hyp.confidence == 0.55
    with pytest.raises(ValidationError):
        Hypothesis(statement="x")  # confidence is required: no silent default
    with pytest.raises(ValidationError, match="statement"):
        Hypothesis(statement="  ", confidence=0.5)
    with pytest.raises(ValidationError):
        Hypothesis(statement="x", confidence=1.5)
    with pytest.raises(ValidationError):
        Hypothesis(statement="x", confidence=-0.1)


def test_action_candidate_execute_requires_tool_or_capability() -> None:
    ok = ActionCandidate(id="cand_c1", kind=CandidateKind.EXECUTE, tool="terminal")
    assert ok.id == "cand_c1"
    observe = ActionCandidate(kind=CandidateKind.OBSERVE)  # information gathering is valid
    assert observe.kind is CandidateKind.OBSERVE
    with pytest.raises(ValidationError, match="tool or capability"):
        ActionCandidate(kind=CandidateKind.EXECUTE)


def test_action_candidate_rejects_model_authored_authority_fields() -> None:
    with pytest.raises(ValidationError):
        ActionCandidate(kind=CandidateKind.EXECUTE, tool="terminal", authorized=True)  # type: ignore[call-overload]
    with pytest.raises(ValidationError):
        ActionCandidate(kind=CandidateKind.EXECUTE, tool="terminal", bypass=True)  # type: ignore[call-overload]


def test_decision_and_recovery_plan_validation() -> None:
    decision = Decision(id="dec_d1", candidate_id="cand_c1", selected=True)
    assert decision.selected is True
    with pytest.raises(ValidationError, match="candidate_id"):
        Decision(candidate_id="  ", selected=True)

    plan = RecoveryPlan(id="rec_r1", failure_id="fail_1", steps=["retry once"])
    assert plan.failure_id == "fail_1"
    with pytest.raises(ValidationError, match="failure_id"):
        RecoveryPlan(failure_id="  ")
    with pytest.raises(ValidationError, match="steps"):
        RecoveryPlan(failure_id="fail_1", steps=[""])


def test_recovery_plan_step_bound() -> None:
    with pytest.raises(ValidationError, match="recovery plan exceeds"):
        RecoveryPlan(failure_id="fail_1", steps=[f"step {i}" for i in range(17)])


def test_world_state_facts_and_kinds() -> None:
    world = WorldState(facts={"db_status": WorldFact(key="db_status", kind=WorldFactKind.UNKNOWN)})
    assert world.facts["db_status"].kind is WorldFactKind.UNKNOWN
    assert world.version == 0
    with pytest.raises(ValidationError, match="key"):
        WorldFact(key="  ")
    assert {k.value for k in WorldFactKind} == {
        "FACT",
        "UNKNOWN",
        "HYPOTHESIS",
        "PREDICTION",
        "VERIFIED_FACT",
        "STALE_FACT",
    }


def test_budget_state_invariant() -> None:
    assert BudgetState().max_steps == 32
    ok = BudgetState(max_steps=5, steps_used=5)  # exhausted is representable
    assert ok.steps_used == ok.max_steps
    with pytest.raises(ValidationError, match="exceeds"):
        BudgetState(max_steps=5, steps_used=6)
    with pytest.raises(ValidationError):
        BudgetState(max_steps=0)


def test_constraints_and_working_memory_bounds() -> None:
    assert Constraints(items=["never touch Project B"]).items == ["never touch Project B"]
    with pytest.raises(ValidationError, match="constraints exceed"):
        Constraints(items=[f"c{i}" for i in range(21)])
    with pytest.raises(ValidationError, match="working memory exceeds"):
        WorkingMemoryView(notes=[f"n{i}" for i in range(17)])


def test_capability_and_memory_ref_validation() -> None:
    assert Capability(name="inspect_dependency_graph", description="d").name
    with pytest.raises(ValidationError, match="name"):
        Capability(name="  ")
    ref = MemoryRef(memory_id="mem_m1", score=0.9)
    assert ref.memory_id == "mem_m1"
    with pytest.raises(ValidationError):
        MemoryRef(memory_id="  ")
    with pytest.raises(ValidationError):
        MemoryRef(memory_id="mem_m1", score=1.5)


def test_stable_ids_and_prefixes() -> None:
    assert Subgoal(goal_id="task_g", description="d").id.startswith("subgoal_")
    assert Hypothesis(statement="s", confidence=0.5).id.startswith("hyp_")
    assert ActionCandidate(kind=CandidateKind.WAIT).id.startswith("cand_")
    assert Decision(candidate_id="c", selected=False).id.startswith("dec_")
    assert RecoveryPlan(failure_id="f").id.startswith("rec_")
    assert len(new_id("mission")) == len("mission_") + 20


def test_canonical_json_is_deterministic() -> None:
    a = ActionCandidate(id="cand_c1", kind=CandidateKind.OBSERVE)
    b = ActionCandidate(id="cand_c1", kind=CandidateKind.OBSERVE)
    assert a == b
    assert canonical_json(a) == canonical_json(b)
    assert canonical_json(a) == canonical_json(a)
    assert ActionCandidate.model_validate_json(canonical_json(a)) == a


def test_brain_package_import_boundary() -> None:
    """The brain foundation may depend only on kernel, contracts, and
    itself: no inference, no authority, no executor, no tools, no
    orchestration, no persistence, no routing — the brain cannot bypass
    or reach around the existing boundaries."""
    brain_dir = Path(__file__).resolve().parents[2] / "src" / "nomadicos" / "brain"
    allowed = {"nomadicos.brain", "nomadicos.contracts", "nomadicos.kernel"}
    files = sorted(brain_dir.rglob("*.py"))
    assert files, "brain package files must exist"
    for path in files:
        source = path.read_text(encoding="utf-8")
        imported = re.findall(r"^\s*(?:from|import)\s+(nomadicos\.[A-Za-z_][\w.]*)", source, re.M)
        for module in imported:
            root = ".".join(module.split(".")[:2])
            assert root in allowed, f"{path.name} imports {module!r} outside the brain boundary"
