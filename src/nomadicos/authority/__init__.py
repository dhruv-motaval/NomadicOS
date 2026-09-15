"""Owner authority brick (SPEC §4-8, §53 Phase 5).

OWNER > POLICY > AUTHORIZATION. This package is the ONLY source of
AuthorizationGrant / AuthorizedAction artifacts. The model consumes
authority; it can never create, extend, reinterpret, or weaken it.
"""

from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.conflicts import OwnerConflictRequest
from nomadicos.authority.policy import CapabilityPolicy, PolicyDecision, PolicyOutcome
from nomadicos.authority.store import (
    AuthorityGrant,
    AuthorityState,
    AuthorityStore,
    OwnerInstruction,
)

__all__ = [
    "AuthorizationService",
    "AuthorityGrant",
    "AuthorityState",
    "AuthorityStore",
    "CapabilityPolicy",
    "OwnerConflictRequest",
    "OwnerInstruction",
    "PolicyDecision",
    "PolicyOutcome",
]
