"""Owner conflict requests (SPEC §5).

The model may request a decision where its action touches an explicit owner
instruction. The request is NOT permission; only the owner can answer it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from nomadicos.contracts.core import Contract
from nomadicos.kernel.errors import AuthorizationDenied
from nomadicos.kernel.ids import new_id

OPTIONS = ("ALLOW", "DENY")


class OwnerConflictRequest(Contract):
    id: str = Field(default_factory=lambda: new_id("task"))
    task_id: str
    step_id: str
    model_id: str
    capability: str
    resource: str
    #: the owner instruction being touched
    conflicting_rule: str
    #: why the model needs it — data, never authority
    model_reason: str = ""
    #: single-use ALLOW binds to this action fingerprint only
    fingerprint: str = ""
    question: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    options: tuple[str, str] = OPTIONS

    def ask(self) -> str:
        return (
            f"{self.question or ('May I access/modify ' + repr(self.resource) + '?')} "
            f"[owner instruction: {self.conflicting_rule!r}] "
            f"(reason: {self.model_reason or 'n/a'}) -> ANSWER ONLY AS ALLOW OR DENY"
        )


def require_owner_resolver(resolver: str) -> None:
    if resolver != "owner":
        raise AuthorizationDenied(
            f"resolver {resolver!r} may not answer owner-conflict requests; owner only",
            resolver=resolver,
        )
