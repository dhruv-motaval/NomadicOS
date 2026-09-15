"""Dispatch-only executor (SPEC §21).

Consumes ONLY ``AuthorizedAction`` (never model output, §56.7). It does not
select models, grant permissions, modify policy, infer capabilities, decide
task success, or call the LLM — enforced structurally (see import-guard test)
:

- artifact binding re-checked (proposal<->grant fingerprint),
- authority EPOCH freshness checked: a revoked/stale grant is refused even
  if it already reached this boundary (SPEC §5 "invalidate promptly"),
- single-use lifecycle: the (action_id, fingerprint) identity is recorded in
  an injected ledger; a second attempt returns the original result marked
  duplicate and NEVER re-executes side effects,
- every tool problem becomes a truthful ExecutionResult; successes here are
  STEP facts only, never task SUCCESS (SPEC §28 boundary preserved).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Protocol

from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.action import AuthorizedAction
from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.kernel.errors import (
    ActionFailed,
    Failure,
    InvalidProposal,
    RevokedAuthority,
)
from nomadicos.kernel.events import EventLogger, EventType
from nomadicos.tools.base import ToolOutcome, ToolRegistry
from nomadicos.tools.context import ExecutionContext
from nomadicos.tools.paths import PathDenied
from nomadicos.tools.terminal import TerminalTool


class DuplicateExecutionBlocked(ActionFailed):
    """Raised only if the caller demands an error; the default path returns."""


class ExecutionLedger(Protocol):
    def get(self, key: str) -> ExecutionResult | None: ...

    def put(self, key: str, result: ExecutionResult) -> None: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._results: dict[str, ExecutionResult] = {}

    def get(self, key: str) -> ExecutionResult | None:
        return self._results.get(key)

    def put(self, key: str, result: ExecutionResult) -> None:
        self._results[key] = result


def action_key(authorized: AuthorizedAction) -> str:
    raw = f"{authorized.id}:{authorized.proposal.fingerprint()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Executor:
    def __init__(
        self,
        tools: ToolRegistry,
        store: AuthorityStore,
        logger: EventLogger,
        ledger: ExecutionLedger | None = None,
    ) -> None:
        self._tools = tools
        self._store = store
        self._log = logger
        self._ledger = ledger or InMemoryLedger()

    async def execute(self, authorized: AuthorizedAction, ctx: ExecutionContext) -> ExecutionResult:
        proposal = authorized.proposal
        grant = authorized.grant
        base = dict(
            task_id=proposal.task_id,
            step_id=proposal.step_id,
            model_id=proposal.model_id,
            tool=proposal.tool,
            capability=f"{proposal.tool}.{proposal.operation}",
        )

        # 1. artifact binding (defense-in-depth; the contract also enforces)
        if grant.proposal_id != proposal.id or grant.fingerprint != proposal.fingerprint():
            raise ActionFailed("AuthorizedAction failed integrity check", **base)

        # 2. freshness: revocation must kill stale grants immediately (§5)
        state = self._store.state_or_empty()
        if grant.authority_epoch != state.epoch or state.grant is None:
            self._log.log(EventType.AUTHORIZATION_DENIED, result="REVOKED_OR_STALE", **base)
            raise RevokedAuthority(
                "grant belongs to a superseded/revoked authority epoch",
                grant_epoch=grant.authority_epoch,
                current_epoch=state.epoch,
            )

        # 3. single-use lifecycle on explicit action identity
        key = action_key(authorized)
        previous = self._ledger.get(key)
        if previous is not None:
            duplicate = previous.model_copy(
                update={
                    "evidence": {**previous.evidence, "duplicate_blocked": True},
                    "message": "action already executed; re-execution refused (SPEC §6.2)",
                }
            )
            self._log.log(EventType.TOOL_EXECUTED, result="DUPLICATE_BLOCKED", **base)
            return duplicate

        # 4. unknown tool/operation cannot execute even past earlier gates
        try:
            tool = self._tools.get(proposal.tool)
            typed = self._tools.check_args(proposal.tool, proposal.operation, proposal.args)
        except InvalidProposal as exc:
            self._log.log(EventType.ACTION_REJECTED, result=str(exc), **base)
            raise

        self._log.log(EventType.TOOL_STARTED, **base)
        try:
            outcome = await tool.run(proposal.operation, typed, ctx)
        except PathDenied as exc:
            # a confinement refusal is an authorization fact, not a tool crash
            self._log.log(EventType.ACTION_REJECTED, result=f"PATH_DENIED: {exc}", **base)
            raise
        except Exception as exc:  # tool crash: structured failure, no re-raise
            outcome = ToolOutcome(
                status=ExecutionStatus.ERRORED,
                failure=Failure.TOOL_ERROR,
                message=f"tool crashed: {type(exc).__name__}: {exc}",
            )
        result = ExecutionResult(
            task_id=proposal.task_id,
            step_id=proposal.step_id,
            action_id=proposal.id,
            action_fingerprint=proposal.fingerprint(),
            model_id=proposal.model_id,
            tool=proposal.tool,
            operation=proposal.operation,
            status=outcome.status,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            evidence=dict(outcome.evidence),
            process_id=outcome.process_id,
            failure=outcome.failure,
            message=outcome.message,
            started_at=datetime.now(UTC),
        )
        self._ledger.put(key, result)
        self._log.log(EventType.TOOL_EXECUTED, result=outcome.status.value, **base)
        return result

    def cleanup_task(self, task_id: str) -> list[int]:
        """Kill only processes owned by this task (SPEC §6.7)."""
        terminal = self._tools.find("terminal")
        if isinstance(terminal, TerminalTool):
            return terminal.supervisor.kill_owned(task_id)
        return []
