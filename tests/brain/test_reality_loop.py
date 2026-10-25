"""Reality loop tests (Sprint 4 — bounded recovery, validation, authority, execution)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nomadicos.action_ir.validation import ProposalValidator, ValidationContext
from nomadicos.authority.authorization import AuthorizationService, OwnerConflictRequest
from nomadicos.authority.policy import CapabilityPolicy
from nomadicos.brain.reality import FailureCategory
from nomadicos.brain.reality_loop import RealityLoop, RealityLoopConfig
from nomadicos.contracts.action import ActionProposal, AuthorizedAction, CapabilityRef
from nomadicos.contracts.core import Goal, parse_predicate
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.contracts.verification import (
    EvidenceItem,
    VerificationLevel,
    VerificationOutcome,
    VerificationResult,
)
from nomadicos.executor.dispatch import Executor, InMemoryLedger
from nomadicos.kernel.events import EventLogger
from nomadicos.tools.context import ExecutionContext
from nomadicos.verification.goal import PredicateGoalVerifier


class FakeCatalog:
    """Minimal ToolCatalog implementation."""

    def has(self, tool: str, operation: str) -> bool:
        return (tool, operation) == ("terminal", "run")

    def capability(self, tool: str, operation: str) -> CapabilityRef:
        return CapabilityRef(capability=f"{tool}.{operation}", resource="*")

    def check_args(self, tool: str, operation: str, args: dict) -> dict:
        return args


class FakeValidator(ProposalValidator):
    def __init__(self) -> None:
        self.calls: list[tuple[ActionProposal, ValidationContext]] = []
        self.should_reject = False
        logger = EventLogger()
        super().__init__(FakeCatalog(), logger)

    def validate(self, proposal: ActionProposal, context: ValidationContext) -> CapabilityRef:
        self.calls.append((proposal, context))
        if self.should_reject:
            raise Exception("validation rejected")
        return super().validate(proposal, context)


class FakeAuthorizer(AuthorizationService):
    def __init__(self, tmp_path: Path) -> None:
        from nomadicos.authority.store import AuthorityStore

        store = AuthorityStore(tmp_path / "auth.json")
        logger = EventLogger()
        policy = CapabilityPolicy(store, granted_patterns=["terminal.*"])
        super().__init__(store, policy, logger)
        store.grant_full_pc_autonomy(source="test_setup")
        self.calls: list[ActionProposal] = []
        self.should_reject = False
        self.return_conflict = False
        self._test_store = store

    def authorize(
        self, proposal: ActionProposal, capability: CapabilityRef
    ) -> AuthorizedAction | OwnerConflictRequest:
        self.calls.append(proposal)
        if self.return_conflict:
            return OwnerConflictRequest(
                task_id=proposal.task_id,
                step_id=proposal.step_id,
                model_id=proposal.model_id,
                capability=capability.capability,
                resource=capability.resource,
                conflicting_rule="test rule",
                model_reason="test reason",
            )
        if self.should_reject:
            raise Exception("authorization rejected")
        return super().authorize(proposal, capability)


class FakeExecutor(Executor):
    def __init__(self, tmp_path: Path) -> None:
        from nomadicos.authority.store import AuthorityStore

        store = AuthorityStore(tmp_path / "auth.json")
        logger = EventLogger()
        super().__init__(None, store, logger, InMemoryLedger())
        self.calls: list[AuthorizedAction] = []
        self.should_fail = False
        self.failure_result: ExecutionResult | None = None

    async def execute(self, authorized: AuthorizedAction, ctx: ExecutionContext) -> ExecutionResult:
        self.calls.append(authorized)
        if self.should_fail:
            raise Exception("executor failed")
        if self.failure_result:
            return self.failure_result
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
            stderr="",
            evidence={"test": True},
            process_id=123,
            message="Success",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )


class FakeVerifier(PredicateGoalVerifier):
    def __init__(self) -> None:
        self.calls: list[tuple[Goal, list[ExecutionResult]]] = []
        self.outcome: VerificationOutcome = VerificationOutcome.PASS

    def verify_task(
        self, goal: Goal, execution_results: list[ExecutionResult]
    ) -> VerificationResult:
        self.calls.append((goal, execution_results))
        passed = self.outcome == VerificationOutcome.PASS
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


@pytest.fixture()
def goal() -> Goal:
    return Goal(
        id="g1",
        objective="Produce out.txt",
        predicates=[parse_predicate({"type": "file_exists", "path": "out.txt"})],
    )


@pytest.fixture()
def candidate() -> dict[str, Any]:
    return {
        "id": "c1",
        "capability": "terminal.run",
        "tool": "terminal",
        "arguments": {"command": "echo hello"},
    }


async def test_success_path(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.action_candidate_id == "c1"
    assert outcome.execution_attempt is not None
    assert outcome.observation is not None
    assert outcome.verification_result is not None
    assert outcome.succeeded
    assert len(v.calls) == 1
    assert len(a.calls) == 1
    assert len(e.calls) == 1
    assert len(ve.calls) == 1


async def test_validation_failure(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    v.should_reject = True
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.VALIDATION_FAILURE
    assert len(a.calls) == 0
    assert len(e.calls) == 0


async def test_authorization_failure(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    a.should_reject = True
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.AUTHORIZATION_FAILURE
    assert len(e.calls) == 0


async def test_owner_conflict(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    a.return_conflict = True
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.AUTHORIZATION_FAILURE
    assert "conflict" in outcome.failure.message.lower()
    assert len(e.calls) == 0


async def test_execution_failure(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    e.should_fail = True
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.EXECUTION_FAILURE


async def test_verification_failure(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    ve.outcome = VerificationOutcome.NOT_PASS
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.VERIFICATION_FAILURE
    assert not outcome.succeeded


async def test_recovery_limit(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    ve.outcome = VerificationOutcome.NOT_PASS
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig(max_recovery_attempts=0))

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.VERIFICATION_FAILURE
    assert outcome.recovery_plan is not None
    assert len(e.calls) == 1


async def test_no_recovery_on_authorization_failure(
    candidate: dict, goal: Goal, tmp_path: Path
) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    a.should_reject = True
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    ve.outcome = VerificationOutcome.NOT_PASS
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig(max_recovery_attempts=2))

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.failure is not None
    assert outcome.failure.category == FailureCategory.AUTHORIZATION_FAILURE
    assert len(e.calls) == 0


async def test_reality_observation_fields(candidate: dict, goal: Goal, tmp_path: Path) -> None:
    v = FakeValidator()
    a = FakeAuthorizer(tmp_path)
    e = FakeExecutor(tmp_path)
    ve = FakeVerifier()
    loop = RealityLoop(v, a, e, ve, RealityLoopConfig())

    outcome = await loop.execute_candidate(candidate, "t1", "s1", "m1", goal)

    assert outcome.observation is not None
    assert outcome.observation.kind == "STEP_RESULT"
    assert outcome.observation.task_id == "t1"
