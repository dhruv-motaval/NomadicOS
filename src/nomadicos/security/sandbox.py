"""Sandbox manager (Sprint 9): policy/data oriented sandbox bindings.

A sandbox is a POLICY RECORD binding an engagement to a target with an
isolation class and resource limits. It grants NO authority — every
action still flows the existing validation/authorization/executor path.
Creation validates the binding deterministically (engagement scope,
target scope, capability intersection non-empty); validation of use is
fail-closed (unknown sandbox, mismatched binding, insufficient isolation
class, unpermitted capability, inactive sandbox).
"""

from __future__ import annotations

from nomadicos.contracts.security import (
    SANDBOX_ID_CHARS,
    Engagement,
    SandboxClass,
    SandboxPolicy,
    SandboxRecord,
    SecurityCapabilityKind,
    TargetRecord,
)
from nomadicos.kernel.errors import InvalidProposal

MAX_SANDBOXES = 64


class SandboxManager:
    def __init__(self, *, max_sandboxes: int = MAX_SANDBOXES) -> None:
        if max_sandboxes < 1:
            raise ValueError("max_sandboxes must be >= 1")
        self._max = max_sandboxes
        self._sandboxes: dict[str, SandboxRecord] = {}

    # ------------------------------------------------------------- create ---
    def create(
        self,
        sandbox_id: str,
        engagement: Engagement,
        target: TargetRecord,
        *,
        sandbox_class: SandboxClass,
        policy: SandboxPolicy | None = None,
        capabilities: list[str] | None = None,
    ) -> SandboxRecord:
        """Deterministic creation: the sandbox is the INTERSECTION of the
        engagement grant and the explicitly given capability list — it can
        never exceed the engagement scope (fail closed when empty)."""
        if len(self._sandboxes) >= self._max:
            raise InvalidProposal(f"sandbox registry full ({self._max})")
        if sandbox_id in self._sandboxes:
            raise InvalidProposal(f"duplicate sandbox {sandbox_id!r}")
        effective_policy = policy if policy is not None else engagement.sandbox_policy
        if capabilities is None:
            granted = list(engagement.allowed_capabilities)
        else:
            granted = list(dict.fromkeys(capabilities))
        unknown = [c for c in granted if c not in engagement.allowed_capabilities]
        if unknown:
            raise InvalidProposal(
                f"sandbox capabilities exceed engagement grant: {unknown}"
            )
        if not granted:
            raise InvalidProposal("a sandbox with no permitted capabilities is invalid")
        sandbox = SandboxRecord(
            sandbox_id=sandbox_id[:SANDBOX_ID_CHARS],
            engagement_id=engagement.engagement_id,
            target_id=target.target_id,
            sandbox_class=sandbox_class,
            policy=effective_policy,
            allowed_capabilities=granted,
            active=True,
        )
        self._sandboxes[sandbox.sandbox_id] = sandbox
        return sandbox

    # ---------------------------------------------------------- validate ---
    def lookup(self, sandbox_id: str) -> SandboxRecord | None:
        return self._sandboxes.get(sandbox_id)

    def validate(
        self,
        sandbox_id: str,
        engagement: Engagement,
        target: TargetRecord,
        capability: SecurityCapabilityKind,
    ) -> tuple[bool, list[str]]:
        """Deterministic sandbox gate (fail closed, explicit reasons)."""
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            return False, [f"unknown sandbox {sandbox_id!r}"]
        reasons: list[str] = []
        if not sandbox.active:
            reasons.append(f"sandbox {sandbox_id!r} is inactive")
        if sandbox.engagement_id != engagement.engagement_id:
            reasons.append(
                f"sandbox bound to engagement {sandbox.engagement_id!r}, "
                f"not {engagement.engagement_id!r}"
            )
        if sandbox.target_id != target.target_id:
            reasons.append(
                f"sandbox bound to target {sandbox.target_id!r}, "
                f"not {target.target_id!r}"
            )
        spec_class = self._required_class(capability)
        if sandbox.sandbox_class.rank < spec_class.rank:
            reasons.append(
                f"capability {capability.value} requires sandbox class "
                f"{spec_class.value}; sandbox is {sandbox.sandbox_class.value}"
            )
        if capability.value not in sandbox.allowed_capabilities:
            reasons.append(
                f"capability {capability.value} not permitted by sandbox {sandbox_id!r}"
            )
        return (not reasons), reasons

    @staticmethod
    def _required_class(capability: SecurityCapabilityKind) -> SandboxClass:
        from nomadicos.contracts.security import SECURITY_CAPABILITIES

        return SECURITY_CAPABILITIES[capability.value].sandbox_class

    def deactivate(self, sandbox_id: str) -> bool:
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None or not sandbox.active:
            return False
        self._sandboxes[sandbox_id] = sandbox.model_copy(update={"active": False})
        return True

    def __len__(self) -> int:
        return len(self._sandboxes)
