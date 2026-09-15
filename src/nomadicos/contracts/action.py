"""Action IR contracts (SPEC §19-21).

The executor consumes `AuthorizedAction` only. Every field that would carry
authority inside a proposal is rejected by the parser, and the proposal
schemas themselves forbid extras so `authorized: true` can never round-trip
through a contract into execution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from nomadicos.contracts.core import Contract
from nomadicos.kernel.ids import new_id

#: Fields that only NomadicOS may derive. Rejected wherever model-authored —
#: top level, nested in args, or any depth (SPEC §19).
MODEL_AUTHORITY_FIELDS = frozenset(
    {
        "authorized",
        "allow",
        "allowed",
        "approve",
        "approved",
        "owner_approved",
        "authorization",
        "capability",
        "capabilities",
        "capability_granted",
        "permission",
        "permissions",
        "privilege",
        "privileges",
        "policy_allowed",
        "execution_allowed",
        "risk",
        "risk_level",
        "bypass",
        "bypass_policy",
        "bypass_security",
        "trusted",
        "system_role",
        "revoke_authority",
        "grant_authority",
        "grant",
        "granted",
        "owner",
        "skip_verification",
        "security_override",
    }
)


class ProposalKind(StrEnum):
    ACTION = "ACTION"
    OBSERVE = "OBSERVE"


class ActionProposal(Contract):
    """Canonical typed model proposal — untrusted intent, not a decision."""

    id: str = Field(default_factory=lambda: new_id("prop"))
    task_id: str
    step_id: str
    attempt: int = 1
    model_id: str
    kind: ProposalKind = ProposalKind.ACTION
    tool: str
    operation: str
    args: dict[str, Any] = Field(default_factory=dict)
    #: The model's own reasoning note; never interpreted as policy input.
    note: str = ""

    def fingerprint(self) -> str:
        """Stable semantic fingerprint: same intent through any surface
        spelling produces the same value (SPEC §31 stuck detection)."""
        canonical = json.dumps(
            {
                "task": self.task_id,
                "step": self.step_id,
                "tool": self.tool,
                "op": self.operation,
                "args": self.args,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CompletionClaim(Contract):
    """`finished = true` is a claim state (SPEC §28), never a decision."""

    id: str = Field(default_factory=lambda: new_id("task"))
    task_id: str
    model_id: str
    claim: bool = True
    justification: str = ""


class AuthorizationGrant(Contract):
    """Authority artifact produced ONLY by the authorization subsystem.

    Carries the authority epoch so revocation invalidates stale grants
    immediately (SPEC §5).
    """

    id: str = Field(default_factory=lambda: new_id("grant"))
    proposal_id: str
    fingerprint: str
    profile: str
    granted_by: Literal["owner", "policy"]
    reason: str
    authority_epoch: int
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthorizedAction(Contract):
    """The only token the executor accepts (SPEC §20-21, §56.7)."""

    id: str = Field(default_factory=lambda: new_id("authz"))
    proposal: ActionProposal
    grant: AuthorizationGrant

    @model_validator(mode="after")
    def _binded(self) -> AuthorizedAction:
        if self.grant.proposal_id != self.proposal.id:
            raise ValueError("grant is not bound to this proposal")
        if self.grant.fingerprint != self.proposal.fingerprint():
            raise ValueError("grant fingerprint does not match proposal — possible tampering")
        return self


class CapabilityRef(Contract):
    """Resolved capability: which tool operation needs which authority (§19)."""

    capability: str  # e.g. "filesystem.write"
    resource: str = ""

    @property
    def namespace(self) -> str:
        return self.capability.split(".", 1)[0]

    @property
    def wildcard(self) -> str:
        return f"{self.namespace}.*"
