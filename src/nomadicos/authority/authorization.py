"""Authorization service — the only mint of AuthorizedAction (SPEC §20, §56.7)."""

from __future__ import annotations

from nomadicos.authority.conflicts import OwnerConflictRequest, require_owner_resolver
from nomadicos.authority.policy import CapabilityPolicy, PolicyDecision
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.action import (
    ActionProposal,
    AuthorizationGrant,
    AuthorizedAction,
    CapabilityRef,
)
from nomadicos.kernel.errors import AuthorizationDenied
from nomadicos.kernel.events import EventLogger, EventType


class AuthorizationService:
    def __init__(
        self,
        store: AuthorityStore,
        policy: CapabilityPolicy,
        logger: EventLogger,
    ) -> None:
        self._store = store
        self._policy = policy
        self._log = logger

    def authorize(
        self, proposal: ActionProposal, cap: CapabilityRef
    ) -> AuthorizedAction | OwnerConflictRequest:
        """Returns an AuthorizedAction, or a pending OwnerConflictRequest.

        Never returns anything for direct model consumption as "permission
        granted" — the caller must actually use the artifact.
        """
        outcome = self._policy.evaluate(cap)
        base = dict(
            task_id=proposal.task_id,
            step_id=proposal.step_id,
            model_id=proposal.model_id,
            tool=proposal.tool,
            capability=cap.capability,
        )
        state = self._store.state_or_empty()
        granted_by = "policy"
        reason = outcome.reason

        if outcome.decision is PolicyDecision.DENY:
            self._log.log(EventType.AUTHORIZATION_DENIED, result=outcome.reason, **base)
            raise AuthorizationDenied(
                outcome.reason, capability=cap.capability, resource=cap.resource
            )

        if outcome.decision is PolicyDecision.ASK_OWNER:
            fingerprint = proposal.fingerprint()
            override = self._store.consume_override(fingerprint)
            if not override:
                if self._store.is_denied(fingerprint):
                    self._log.log(
                        EventType.AUTHORIZATION_DENIED,
                        result="owner previously DENIED this exact action",
                        **base,
                    )
                    raise AuthorizationDenied(
                        "owner previously denied this exact action; it remains blocked",
                        capability=cap.capability,
                        resource=cap.resource,
                    )
                request = OwnerConflictRequest(
                    task_id=proposal.task_id,
                    step_id=proposal.step_id,
                    model_id=proposal.model_id,
                    capability=cap.capability,
                    resource=cap.resource,
                    conflicting_rule=(outcome.instruction.rule if outcome.instruction else ""),
                    model_reason=proposal.note,
                    fingerprint=proposal.fingerprint(),
                )
                self._store.record_conflict(request.id, request.model_dump(mode="json"))
                self._log.log(EventType.OWNER_CONFLICT_REQUESTED, result="PENDING", **base)
                return request
            granted_by = "owner"
            reason = "owner explicit override (single-use)"
        grant = AuthorizationGrant(
            proposal_id=proposal.id,
            fingerprint=proposal.fingerprint(),
            profile=state.grant.profile if state.grant else "NONE",
            granted_by=granted_by,  # type: ignore[arg-type]
            reason=reason,
            authority_epoch=state.epoch,
        )
        authorized = AuthorizedAction(proposal=proposal, grant=grant)
        self._log.log(EventType.AUTHORIZATION_GRANTED, result=reason, **base)
        return authorized

    def answer_conflict(self, request_id: str, decision: str, *, resolver: str) -> None:
        """Owner-only endpoint. Any other caller is a hard denial (§4)."""
        require_owner_resolver(resolver)
        answer = self._store.answer_conflict(request_id, decision, resolver=resolver)
        self._log.log(
            EventType.OWNER_DECISION, result=answer.decision, payload={"request_id": request_id}
        )

    def pending_conflicts(self) -> list[OwnerConflictRequest]:
        return [
            OwnerConflictRequest.model_validate(payload)
            for payload in self._store.state().conflicts.values()
        ]

    def revoke_all(self) -> int:
        state = self._store.revoke_all()
        self._log.log(
            EventType.PERMISSION_REVOKED, result="REVOKE ALL ACCESS", payload={"epoch": state.epoch}
        )
        return state.epoch

    def grant_full(self, source: str = "owner_cli") -> None:
        state = self._store.grant_full_pc_autonomy(source=source)
        self._log.log(
            EventType.PERMISSION_GRANTED,
            result="FULL_PC_AUTONOMY",
            payload={"epoch": state.epoch, "source": source},
        )
