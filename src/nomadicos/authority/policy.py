"""Capability + owner-instruction policy (SPEC §5-8, §6 security split).

Pure decision function over (capability, resource, persistent owner state).
It knows nothing about models or executors; it cannot be asked, only used.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nomadicos.authority.store import AuthorityState, AuthorityStore, OwnerInstruction
from nomadicos.contracts.action import CapabilityRef


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    #: model may ask the owner; never resolved by the model (§5)
    ASK_OWNER = "ASK_OWNER"


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    reason: str
    capability: str
    resource: str
    instruction: OwnerInstruction | None = None


def normalize_resource(resource: str) -> str:
    return resource.replace("\\", "/").casefold().rstrip("/")


def resource_matches(pattern: str, resource: str) -> bool:
    """Owner-rule containment on normalized paths/names (owner semantics).

    Deliberately substring-based: "Project B" must match
    "C:\\Users\\dhruv\\src\\Project B\\main.py". Filesystem *confinement*
    (Phase 6) uses resolved realpaths instead — different boundary, stricter.
    """
    if not pattern:
        return False
    p = normalize_resource(pattern)
    r = normalize_resource(resource)
    return p in r or r in p and len(r) >= len(p)


class CapabilityPolicy:
    """Answers: is this capability authorized under current owner state?"""

    def __init__(
        self,
        store: AuthorityStore,
        *,
        granted_patterns: list[str],
        hard_denied_resources: list[str] | None = None,
    ) -> None:
        self._store = store
        self._patterns = list(granted_patterns)
        self._hard_denied = list(hard_denied_resources or [])

    def _pattern_match(self, cap: CapabilityRef) -> bool:
        for pattern in self._patterns:
            if pattern == "full":
                return True
            ns, _, op = pattern.partition(".")
            if ns != cap.namespace:
                continue
            if op == "*":
                return True
            return pattern == cap.capability
        return False

    def evaluate(self, cap: CapabilityRef, state: AuthorityState | None = None) -> PolicyOutcome:
        state = state if state is not None else self._store.state_or_empty()
        out = lambda d, r, i=None: PolicyOutcome(d, r, cap.capability, cap.resource, i)  # noqa: E731

        if not self._patterns:
            return out(PolicyDecision.DENY, "no capability patterns configured; fail closed")
        if state.grant is None:
            return out(PolicyDecision.DENY, "no owner grant (FULL_PC_AUTONOMY not given)")
        for denied in self._hard_denied:
            if resource_matches(denied, cap.resource):
                return out(PolicyDecision.DENY, f"resource hard-denied by owner config: {denied}")
        for instruction in state.instructions:
            if resource_matches(instruction.resource, cap.resource):
                return out(
                    PolicyDecision.ASK_OWNER,
                    f"conflicts with owner instruction: {instruction.rule!r}",
                    instruction,
                )
        if not self._pattern_match(cap):
            #: unknown capability => DENY, never implicit success (§56.14)
            return out(
                PolicyDecision.DENY, f"capability {cap.capability} not inside active profile"
            )
        return out(PolicyDecision.ALLOW, f"capability granted by profile {state.grant.profile}")
