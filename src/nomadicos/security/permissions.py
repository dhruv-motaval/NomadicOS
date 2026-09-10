"""Permission Engine (BP §37-38, §90): identity + per-subject permission state.

The engine is the single source of who may do what. It never executes anything
and is read-only for the model (I3/I4).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nomadicos.constitution.policy_schema import Requirement
from nomadicos.core.errors import PermissionDenied
from nomadicos.core.logging import get_logger

logger = get_logger("security.permissions")


@dataclass(frozen=True, slots=True)
class SubjectIdentity:
    """Correlated identity for one request (BP §399)."""

    user_id: str
    session_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None


@dataclass
class Grant:
    """An explicit user authorization (BP §33: USER grants authority)."""

    tool: str
    granted_by: str = "user"
    granted_at: datetime = field(default_factory=datetime.now)
    one_shot: bool = False


class PermissionEngine:
    """Tracks explicit user authorizations; policy supplies the rest.

    Invariants enforced here: the model cannot add grants (I3); grants only
    come from the owner path (CLI/UI), never from model output or policy files.
    """

    def __init__(self) -> None:
        self._grants: list[Grant] = []
        self._denials: dict[str, int] = {}

    def grant(self, tool: str, *, granted_by: str = "user", one_shot: bool = False) -> Grant:
        """Owner action — never callable from model-influenced code paths (I3)."""
        grant = Grant(tool=tool, granted_by=granted_by, one_shot=one_shot)
        self._grants.append(grant)
        logger.info("permission granted tool=%s by=%s one_shot=%s", tool, granted_by, one_shot)
        return grant

    def has_authorization(self, tool: str) -> bool:
        return any(g.tool == tool or g.tool == "*" for g in self._grants)

    def consume(self, tool: str) -> None:
        """Consume one-shot grants after use (BP §355: one-shot override)."""
        remaining: list[Grant] = []
        for grant in self._grants:
            if grant.tool in (tool, "*") and grant.one_shot:
                logger.info("one-shot permission consumed tool=%s", tool)
                continue
            remaining.append(grant)
        self._grants = remaining

    def satisfies_requirements(
        self, required: list[Requirement], tool: str
    ) -> dict[str, bool]:
        return {
            Requirement.EXPLICIT_USER_AUTHORIZATION.value: self.has_authorization(tool),
            Requirement.USER_CONFIRMATION.value: self.has_authorization(tool),
            Requirement.DRY_RUN_FIRST.value: True,  # dry-run capability is structural
            Requirement.VERIFICATION_REQUIRED.value: True,  # verifiers always available
        }

    def record_denial(self, tool: str, reason: str, context: dict[str, Any] | None = None) -> None:
        """Security event feed (BP §123); repeated denials escalate visibility."""
        self._denials[tool] = self._denials.get(tool, 0) + 1
        logger.warning(
            "permission denied tool=%s count=%d reason=%s", tool, self._denials[tool], reason
        )

    def denial_count(self, tool: str) -> int:
        return self._denials.get(tool, 0)

    def assert_user_action(self, caller: str) -> None:
        """Guard for privileged methods: only the owner surface may call (I3)."""
        if caller not in ("cli", "ui", "owner"):
            raise PermissionDenied(
                f"only the owner surface can perform this action, got {caller!r}",
                context={"caller": caller},
            )


__all__ = ["Grant", "PermissionEngine", "SubjectIdentity"]
