"""Proposal validation (SPEC §19: VALIDATION happens before capability
resolution, authorization, and execution)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from nomadicos.contracts.action import ActionProposal, CapabilityRef
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.kernel.events import EventLogger, EventType


class ToolCatalog(Protocol):
    """What the tool layer exposes to the IR boundary.

    Implemented by the executor's ToolRegistry (Phase 6); unknown actions can
    never pass validation regardless of what a model requested.
    """

    def has(self, tool: str, operation: str) -> bool: ...

    def capability(self, tool: str, operation: str) -> CapabilityRef: ...

    def check_args(self, tool: str, operation: str, args: dict) -> dict:
        """Return typed/validated args or raise InvalidProposal."""
        ...


class ValidationContext(BaseModel):
    """System-side identity expectations. Proposal identity must match."""

    task_id: str
    step_id: str
    model_id: str
    attempt: int = 1


class ProposalValidator:
    def __init__(self, catalog: ToolCatalog, logger: EventLogger) -> None:
        self._catalog = catalog
        self._logger = logger

    def validate(self, proposal: ActionProposal, context: ValidationContext) -> CapabilityRef:
        """Validate and resolve the capability a policy decision will need.

        Raises InvalidProposal; never returns something executable itself.
        """
        log = self._logger
        base = dict(
            task_id=proposal.task_id,
            step_id=proposal.step_id,
            attempt=proposal.attempt,
            model_id=proposal.model_id,
            tool=proposal.tool,
        )
        log.log(EventType.ACTION_PROPOSED, result="PARSED", **base)
        if proposal.task_id != context.task_id:
            self._reject(
                log, f"task identity mismatch: {proposal.task_id} != {context.task_id}", base=base
            )
        if proposal.step_id != context.step_id:
            self._reject(
                log, f"step identity mismatch: {proposal.step_id} != {context.step_id}", base=base
            )
        if proposal.model_id != context.model_id:
            self._reject(
                log,
                f"model identity mismatch: {proposal.model_id} != {context.model_id}",
                base=base,
            )
        if proposal.attempt != context.attempt:
            self._reject(
                log, f"attempt mismatch: {proposal.attempt} != {context.attempt}", base=base
            )
        if proposal.id.startswith(("grant_", "authz_")):
            self._reject(log, "proposal ids must not impersonate authority artifacts", base=base)
        if not self._catalog.has(proposal.tool, proposal.operation):
            self._reject(log, f"unknown action {proposal.tool}.{proposal.operation}", base=base)
        try:
            typed_args = self._catalog.check_args(proposal.tool, proposal.operation, proposal.args)
        except InvalidProposal as exc:
            self._reject(log, f"arguments invalid: {exc.message}", base=base)
            raise
        # The proposal is NOT mutated: validation proves the canonical IR
        # stable (fingerprints, checkpoints, single-use keys); the executor
        # re-checks typed args again before dispatch.
        _ = typed_args
        ref = self._catalog.capability(proposal.tool, proposal.operation)
        resource = _resource_hint(proposal.args)
        return CapabilityRef(capability=ref.capability, resource=resource)

    @staticmethod
    def _reject(log: EventLogger, reason: str, *, base: dict) -> None:
        log.log(
            EventType.ACTION_REJECTED, result="INVALID_PROPOSAL", payload={"reason": reason}, **base
        )
        raise InvalidProposal(reason)


def _resource_hint(args: dict) -> str:
    for key in ("path", "source", "directory", "command", "target", "url", "title"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
