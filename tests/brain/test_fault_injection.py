"""Sprint 10 — fault injection: every boundary fails safely. Failures are
deterministic, auditable, and never become fabricated success."""

from __future__ import annotations

import tempfile
from datetime import datetime
from itertools import count
from pathlib import Path

import pytest

from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.store import AuthorityStore
from nomadicos.brain.adaptive import AdaptiveController
from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.reality import FailureCategory
from nomadicos.brain.reality_loop import RealityLoop, RealityLoopConfig
from nomadicos.brain.state import BrainState
from nomadicos.contracts.action import ActionProposal, CapabilityRef
from nomadicos.contracts.core import Goal
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.memory import MemoryKind, MemoryProvenance, MemoryQuery, MemoryRecord
from nomadicos.contracts.skills import SkillInvocation, SkillQuery
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.kernel.events import EventLogger
from nomadicos.memory.retrieval import Retriever
from nomadicos.memory.store import JsonlMemoryStore
from nomadicos.skills.registry import SkillRegistry
from nomadicos.skills.retrieval import rank_skills
from nomadicos.skills.validation import validate_invocation
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.filesystem import FilesystemTool


def state() -> BrainState:
    goal = Goal(id="task_g", objective="fix the auth module", predicates=[])
    return BrainState.for_goal(goal, task_graph=TaskGraph.build(goal_id="task_g"))


# Local fake classes for RealityLoop tests (avoid importing from tests module)
class _FakeCatalog:
    def has(self, tool, op):
        return (tool, op) == ("terminal", "run")

    def capability(self, tool, op):
        return CapabilityRef(capability=f"{tool}.{op}", resource="*")

    def check_args(self, tool, op, args):
        return args


class _FakeValidator(ProposalValidator):
    def __init__(self):
        from nomadicos.kernel.events import EventLogger
        super().__init__(_FakeCatalog(), EventLogger())


class _FakeAuthorizer(AuthorizationService):
    def __init__(self, tmp_path: Path):
        store = AuthorityStore(tmp_path / "auth.json")
        from nomadicos.authority.policy import CapabilityPolicy
        policy = CapabilityPolicy(store, granted_patterns=["terminal.*"])
        super().__init__(store, policy, EventLogger())
        store.grant_full_pc_autonomy(source="test")


class _FakeExecutor:
    async def execute(self, authorized, ctx):
        return ExecutionResult(
            task_id=authorized.proposal.task_id,
            step_id=authorized.proposal.step_id,
            action_id=authorized.proposal.id,
            action_fingerprint=authorized.proposal.fingerprint(),
            model_id=authorized.proposal.model_id,
            tool=authorized.proposal.tool,
            operation=authorized.proposal.operation,
            status=ExecutionStatus.SUCCEEDED,
            exit_code=0,
            stdout="ok",
            evidence={"test": True},
            message="ok",
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )


class _FakeVerifier:
    def __init__(self):
        from nomadicos.contracts.verification import VerificationOutcome
        self.outcome = VerificationOutcome.PASS

    def verify_task(self, goal, execution_results):
        from nomadicos.contracts.verification import (
            EvidenceItem,
            VerificationLevel,
            VerificationOutcome,
            VerificationResult,
        )
        passed = self.outcome is VerificationOutcome.PASS
        evidence = [
            EvidenceItem(
                claim="test",
                observed=passed,
                detail={},
            )
        ]
        return VerificationResult(
            id="v_1",
            level=VerificationLevel.STEP,
            task_id=goal.id,
            step_id="s1",
            evidence=evidence,
            outcome=self.outcome,
            verifier="test",
        )


def _reality_loop(tmp_path: Path, executor, verifier):
    return RealityLoop(
        _FakeValidator(),
        _FakeAuthorizer(tmp_path),
        executor,
        verifier,
        RealityLoopConfig(max_recovery_attempts=0),
    )


class ExplodingRetriever:
    """MemoryRetriever protocol stub that always fails."""

    def retrieve(self, query):
        raise RuntimeError("memory brick down")


def test_memory_failure_degrades_fail_safe() -> None:
    """Sprint 6 adaptive pass CONTINUES without memory when memory fails
    (fail-safe degraded cognition); memory-less run stays deterministic."""
    seq = count(1)
    controller = AdaptiveController(
        memory=ExplodingRetriever(),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 1, 1),
        event_id=lambda: f"evt_{next(seq):06d}",
    )
    updated = controller.run(state())
    assert updated.attention is not None  # pass continued
    assert updated.relevant_memories == []  # no fabricated memories
    assert updated.metacognition is not None


def test_memory_retrieval_failure_is_honest(tmp_path) -> None:
    """The retriever surfaces store failures honestly; the fail-safe
    boundary is the ADAPTIVE layer (tested above)."""

    class ExplodingStore:
        def records(self):
            raise OSError("disk gone")

    with pytest.raises(OSError):
        Retriever(ExplodingStore()).retrieve(MemoryQuery(text="x"))  # type: ignore[arg-type]


def test_memory_persistence_failure_is_not_silent(tmp_path) -> None:
    store = JsonlMemoryStore(tmp_path / "memory.jsonl")
    # a DIRECTORY as the file target: the atomic replace must fail loudly
    store.path = Path(tmp_path)
    with pytest.raises(OSError):
        store.write(
            MemoryRecord(
                id="mem_x",
                kind=MemoryKind.SEMANTIC,
                content="c",
                provenance=MemoryProvenance(confidence=0.5),
            )
        )


def test_routing_failure_is_deterministic_fail_safe() -> None:
    from nomadicos.brain.cognition import CognitiveRole
    from nomadicos.brain.models import role_profile
    from nomadicos.kernel.config import RouterConfig
    from nomadicos.router.cognitive import CognitiveRoleRouter
    from nomadicos.router.selection import ModelSelector

    class ExplodingRegistry:
        def enabled(self):
            raise RuntimeError("registry gone")

    router = CognitiveRoleRouter(
        ModelSelector(RouterConfig()),
        ExplodingRegistry(),  # type: ignore[arg-type]
    )
    data = router.select_for_role(role_profile(CognitiveRole.DECOMPOSER))
    assert data["available"] is False  # explicit deterministic failure
    assert "failed closed" in data["reasons"][0] or "unavailable" in data["reasons"][0]
    assert data["model_id"] == ""  # never an unqualified selection


def test_skill_retrieval_failure_is_deterministic(tmp_path) -> None:
    class ExplodingIndex:
        def by_capability(self, capability):
            raise RuntimeError("index corrupted")

    from nomadicos.skills.retrieval import rank_skills as broken_rank

    with pytest.raises(RuntimeError):
        broken_rank(ExplodingIndex(), SkillQuery(capability="filesystem.read"))  # type: ignore[arg-type]
    # the REGISTRY path itself never fails silently: unknown capability → empty
    result = rank_skills(SkillRegistry(), SkillQuery(capability="ghost.op"))
    assert result.matches == [] and result.excluded == []


def test_skill_validation_unknown_skill_fails_closed(tmp_path) -> None:
    registry = SkillRegistry()
    result = validate_invocation(
        registry,
        SkillInvocation(skill_id="ghost", version=1, args={}),
    )
    assert result.ok is False and "unknown skill" in result.reasons[0]


def test_validator_rejects_unknown_action() -> None:
    catalog = ToolRegistry()
    catalog.register(FilesystemTool())
    validator = ProposalValidator(catalog, EventLogger())

    proposal = ActionProposal(
        task_id="task_1", step_id="s1", model_id="m",
        tool="filesystem", operation="disintegrate", args={},
    )
    with pytest.raises(InvalidProposal):
        validator.validate(
            proposal, ValidationContext(task_id="task_1", step_id="s1", model_id="m")
        )


def test_executor_failure_becomes_failure_state_not_success() -> None:
    """Sprint 4 reality loop: an executor exception yields an ERRORED
    attempt + FAILURE outcome — never a fabricated success."""

    class ExplodingExecutor:
        async def execute(self, authorized, ctx):
            raise RuntimeError("executor down")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        loop = RealityLoop(
            _FakeValidator(),
            _FakeAuthorizer(tmp_path),
            ExplodingExecutor(),  # type: ignore[arg-type]
            _FakeVerifier(),
            RealityLoopConfig(max_recovery_attempts=0),
        )
        import asyncio

        outcome = asyncio.run(
            loop.execute_candidate(
                {"id": "c1", "capability": "terminal.run", "tool": "terminal",
                 "arguments": {"command": "echo"}},
                "task_1", "s1", "m1",
                Goal(id="task_g", objective="obj", predicates=[]),
            )
        )
        assert outcome.succeeded is False
        assert outcome.failure is not None
        assert outcome.failure.category in (
            FailureCategory.EXECUTION_FAILURE,
            FailureCategory.AUTHORIZATION_FAILURE,
        )


def test_verification_failure_is_not_success(tmp_path) -> None:
    """Execution succeeded, verification FAILED → outcome NOT success."""

    class _FakeVerifier:
        def __init__(self):
            self.outcome = __import__(
                "nomadicos.contracts.verification", fromlist=["VerificationOutcome"]
            ).VerificationOutcome.NOT_PASS

        def verify_task(self, goal, execution_results):
            from nomadicos.contracts.verification import (
                EvidenceItem,
                VerificationLevel,
                VerificationOutcome,
                VerificationResult,
            )
            return VerificationResult(
                id="v_1",
                level=VerificationLevel.STEP,
                task_id="task_1",
                step_id="s1",
                evidence=[
                    EvidenceItem(
                        claim="test",
                        observed=False,
                        detail={},
                    )
                ],
                outcome=VerificationOutcome.NOT_PASS,
                verifier="test",
            )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        verifier = _FakeVerifier()
        loop = RealityLoop(
            _FakeValidator(),
            _FakeAuthorizer(tmp_path),
            _FakeExecutor(),
            verifier,
            RealityLoopConfig(max_recovery_attempts=0),
        )
        import asyncio

        outcome = asyncio.run(
            loop.execute_candidate(
                {"id": "c1", "capability": "terminal.run", "tool": "terminal",
                 "arguments": {"command": "echo"}},
                "task_1", "s1", "m1",
                Goal(id="task_g", objective="obj", predicates=[]),
            )
        )
        assert outcome.succeeded is False  # execution ok, verification FAILED
        assert outcome.verification_result.outcome.value == "NOT_PASS"


def test_audit_failure_is_counted_not_silent(tmp_path) -> None:
    """Sprint 10 hardening: a failing durable sink is COUNTED (never a
    silent data-loss illusion)."""

    def exploding_sink(event):
        raise OSError("audit disk full")

    logger = EventLogger(sink=exploding_sink)
    from nomadicos.kernel.events import EventType

    logger.log(EventType.ACTION_PROPOSED, task_id="t", result="r")
    assert logger.dropped_events == 1  # counted, not silent
    logger.log(EventType.ACTION_PROPOSED, task_id="t", result="r")
    assert logger.dropped_events == 2
    assert len(logger.events("t")) == 2  # in-memory trail intact
