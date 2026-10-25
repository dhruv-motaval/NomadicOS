"""Engagement validation (Sprint 9): scope is EXPLICIT and must be ACTIVE.

Only ACTIVE engagements may authorize security research; suspended,
completed, revoked, and draft engagements fail closed. Capability and
target scope checks live here; authorization proper stays with the
existing AuthorizationService downstream.
"""

from __future__ import annotations

from nomadicos.contracts.security import (
    Engagement,
    EngagementStatus,
    SecurityCapabilityKind,
)


def validate_engagement(
    engagement: Engagement | None,
    capability: SecurityCapabilityKind,
    target_id: str,
) -> tuple[bool, list[str]]:
    """Deterministic engagement gate. Returns (ok, reasons); every failure
    is explicit (never silently re-scoped to another target/capability)."""
    if engagement is None:
        return False, ["missing engagement: no security research without explicit scope"]
    reasons: list[str] = []
    if engagement.status is not EngagementStatus.ACTIVE:
        reasons.append(
            f"engagement {engagement.engagement_id} is "
            f"{engagement.status.value}; only ACTIVE engagements authorize research"
        )
    if capability.value not in engagement.allowed_capabilities:
        reasons.append(
            f"capability {capability.value} not granted by engagement {engagement.engagement_id}"
        )
    if target_id not in engagement.allowed_targets:
        reasons.append(f"target {target_id!r} outside engagement {engagement.engagement_id} scope")
    return (not reasons), reasons
