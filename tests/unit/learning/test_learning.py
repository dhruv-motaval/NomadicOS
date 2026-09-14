"""Phase 13 tests: self-improvement lifecycle (BP §29-31, §67, §147, §216, §243, §289)."""

import pytest

from nomadicos.core.errors import ImprovementRejected, SecurityPolicyViolation
from nomadicos.learning.engine import LearningEngine
from nomadicos.learning.store import (
    CandidateKind,
    CandidateState,
    ImprovementStore,
)


@pytest.fixture()
def engine() -> LearningEngine:
    return LearningEngine(ImprovementStore())


async def test_propose_records_candidate(engine: LearningEngine) -> None:
    candidate = await engine.propose(
        CandidateKind.MODEL_SELECTION_UPDATE,
        "Prefer model/coder for coding tasks based on 6 verified successes",
        {"task_family": "coding", "preferred_model": "model/coder"},
        source="experience analysis",
    )
    assert candidate.state is CandidateState.PROPOSED
    assert candidate.kind is CandidateKind.MODEL_SELECTION_UPDATE


@pytest.mark.security
async def test_invariant_violating_proposal_rejected(engine: LearningEngine) -> None:
    """I4: a proposal can never carry privileged grants (BP §260, §372)."""
    with pytest.raises(SecurityPolicyViolation):
        await engine.propose(
            CandidateKind.AGENT_POLICY_UPDATE,
            "Self-grant policy modification",
            {"allow_policy_modification": True},
            source="model",
        )


async def test_sandbox_failure_rejects(engine: LearningEngine) -> None:
    candidate = await engine.propose(
        CandidateKind.WORKFLOW_UPDATE, "Bad workflow", {"step": "x"}, source="test"
    )

    async def broken_sandbox(payload):
        raise RuntimeError("sandbox crash")

    with pytest.raises(ImprovementRejected, match="sandbox"):
        await engine.run_sandboxed(candidate, broken_sandbox)
    assert candidate.state is CandidateState.REJECTED


async def test_promotion_requires_beating_baseline(engine: LearningEngine) -> None:
    """BP §216: new success rate must beat old; 'looks better' is not enough."""
    candidate = await engine.propose(
        CandidateKind.MODEL_SELECTION_UPDATE, "Better selection", {"w": 1}, source="test"
    )
    await engine.run_sandboxed(candidate, lambda payload: None)
    candidate.benchmark_score = 7.0
    candidate.baseline_score = 8.0  # candidate is WORSE
    candidate.security_passed = True
    decision = await engine.decide(candidate)
    assert decision.promoted is False
    assert "benchmark" in decision.reason.lower()


async def test_promotion_requires_security_pass(engine: LearningEngine) -> None:
    candidate = await engine.propose(
        CandidateKind.MODEL_SELECTION_UPDATE, "Faster selection", {"w": 1}, source="test"
    )
    await engine.run_sandboxed(candidate, lambda payload: None)
    candidate.benchmark_score = 9.0
    candidate.baseline_score = 7.0
    candidate.security_passed = False  # BP §147: security first (§371)
    decision = await engine.decide(candidate)
    assert decision.promoted is False


async def test_full_promotion_lifecycle_with_versioning(engine: LearningEngine) -> None:
    """BP §29-31: sandbox → benchmark → promote → version → rollback path."""
    # Baseline candidate: parent version established first.
    parent = await engine.propose(
        CandidateKind.MODEL_SELECTION_UPDATE, "Initial selection policy", {"w": 0}, source="seed"
    )
    await engine.run_sandboxed(parent, lambda payload: None)
    parent.benchmark_score = 7.0
    parent.baseline_score = 6.0
    parent.security_passed = True
    parent_decision = await engine.decide(parent)
    assert parent_decision.promoted is True

    child = await engine.propose(
        CandidateKind.MODEL_SELECTION_UPDATE,
        "Improved weights after 10 verified runs",
        {"w": 2},
        source="experience analysis",
    )
    child.parent_version = parent_decision.version_id  # rollback target (BP §44)
    await engine.run_sandboxed(child, lambda payload: None)
    child.benchmark_score = 9.0
    child.baseline_score = 7.0
    child.security_passed = True
    child_decision = await engine.decide(child)
    assert child_decision.promoted is True

    # BP §289: regression detected → rollback restores the last known-good (parent)
    healthy = await engine.monitor(child_decision.version_id, lambda: False)
    assert healthy is False
    restored = await engine.rollback(child_decision.version_id)
    assert restored["version_id"] == parent_decision.version_id
    assert restored["payload"] == {"w": 0}
    assert child.state is CandidateState.ROLLED_BACK
    active = await engine._store.active()
    assert active is not None and active["payload"] == {"w": 0}


async def test_learning_pause_on_resource_pressure(engine: LearningEngine) -> None:
    """BP §245: foreground work has priority."""
    engine.pause_learning()
    candidate = await engine.propose(CandidateKind.WORKFLOW_UPDATE, "x", {"a": 1}, source="test")
    with pytest.raises(ImprovementRejected, match="paused"):
        await engine.run_sandboxed(candidate, lambda payload: None)
    engine.resume_learning()
    sandboxed = await engine.run_sandboxed(candidate, lambda payload: None)
    assert sandboxed.state is CandidateState.SANDBOXED
