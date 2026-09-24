"""Persistent owner authority state (SPEC §5, §47; §53 Phase 5.1-5.4).

State lives in one JSON file written atomically. Semantics:

- missing file  -> NO authority at all (a restart never grants, §5.3).
- corrupt/invalid state -> treated as NO authority AND reported loudly;
  the system must never silently assume FULL_PC_AUTONOMY (fail closed).
- every grant mutation bumps ``authority_epoch``; revocation bumps it and
  clears all overrides. The executor checks the epoch on each action so an
  in-flight grant is invalidated "as quickly as practical" after revoke (§5).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, ValidationError

from nomadicos.contracts.core import Contract
from nomadicos.kernel.errors import ConfigInvalid
from nomadicos.kernel.ids import new_id


def _replace_with_retry(tmp: Path, target: Path, attempts: int = 4) -> None:
    """os.replace can transiently fail on Windows (AV/OneDrive locks);
    bounded retry keeps atomicity without masking real failures."""
    import time

    for attempt in range(3):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(0.05 * (2**attempt))
    os.replace(tmp, target)


class AuthorityGrant(Contract):
    profile: str
    granted_at: datetime
    #: the ONLY acceptable author of a grant
    granted_by: str = "owner"
    #: which owner surface recorded it (cli, first_run_dialog, ...)
    source: str = "owner"


class OwnerInstruction(Contract):
    """An explicit owner rule, e.g. 'Do not touch Project B' (§5, §8)."""

    id: str = Field(default_factory=lambda: new_id("task"))
    rule: str
    #: resource substring the rule covers (path, project name, ...)
    resource: str = ""


class OwnerAnswer(Contract):
    decision: str  # ALLOW | DENY
    answered_by: str = "owner"
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AuthorityState(Contract):
    version: int = 1
    epoch: int = 1
    grant: AuthorityGrant | None = None
    instructions: list[OwnerInstruction] = Field(default_factory=list)
    #: pending owner-conflict requests, by id
    conflicts: dict[str, dict] = Field(default_factory=dict)
    #: fingerprints the owner explicitly ALLOWed (single-use overrides)
    override_allowed: dict[str, OwnerAnswer] = Field(default_factory=dict)
    #: fingerprints the owner DENIED — asking the same question again is
    #: not allowed to loop the owner into submission (SPEC §5 "DENY -> remain blocked")
    denied_fingerprints: dict[str, OwnerAnswer] = Field(default_factory=dict)
    last_revoked_at: datetime | None = None


class AuthorityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------- load ---
    def state(self) -> AuthorityState:
        """Never raises for missing/corrupt data; corrupt => zero authority."""
        if not self.path.exists():
            return AuthorityState()
        try:
            return AuthorityState.model_validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError, TypeError) as exc:
            raise ConfigInvalid(f"authority state unreadable; refusing all grants: {exc}") from exc

    def state_or_empty(self) -> AuthorityState:
        try:
            return self.state()
        except ConfigInvalid:
            return AuthorityState()

    def _write(self, state: AuthorityState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state.model_dump(mode="json"), fh, ensure_ascii=False)
            _replace_with_retry(Path(tmp), self.path)
        finally:
            if os.path.exists(tmp):  # pragma: no cover - only on failure
                os.unlink(tmp)

    # ---------------------------------------------------------- mutate ---
    def grant_full_pc_autonomy(self, *, source: str = "owner_cli") -> AuthorityState:
        state = self.state()
        if state.grant is not None and state.grant.profile == "FULL_PC_AUTONOMY":
            return state  # persistent: already granted, later tasks inherit (§5)
        state.grant = AuthorityGrant(
            profile="FULL_PC_AUTONOMY", granted_at=datetime.now(UTC), source=source
        )
        state.epoch += 1  # new epoch: fresh validity window
        state.last_revoked_at = None
        self._write(state)
        return state

    def revoke_all(self) -> AuthorityState:
        """Master revoke: no operational autonomy, all overrides void (§5)."""
        state = self.state()
        state.grant = None
        state.override_allowed.clear()
        state.conflicts.clear()
        state.epoch += 1  # invalidates every outstanding grant artifact
        state.last_revoked_at = datetime.now(UTC)
        self._write(state)
        return state

    def add_instruction(self, rule: str, resource: str = "") -> OwnerInstruction:
        state = self.state()
        instruction = OwnerInstruction(rule=rule, resource=resource)
        state.instructions = [i for i in state.instructions if i.rule != rule] + [instruction]
        self._write(state)
        return instruction

    def remove_instruction(self, instruction_id: str) -> None:
        state = self.state()
        state.instructions = [i for i in state.instructions if i.id != instruction_id]
        self._write(state)

    # ------------------------------------------------- conflict storage ---
    def record_conflict(self, request_id: str, payload: dict) -> None:
        state = self.state()
        state.conflicts[request_id] = payload
        self._write(state)

    def answer_conflict(self, request_id: str, decision: str, *, resolver: str) -> OwnerAnswer:
        """Only the owner may answer. resolver must literally be 'owner'."""
        if resolver != "owner":
            raise ConfigInvalid("only the owner can answer authority conflicts")
        if decision.upper() not in {"ALLOW", "DENY"}:
            raise ConfigInvalid("decision must be ALLOW or DENY")
        state = self.state()
        conflict = state.conflicts.pop(request_id, None)
        if conflict is None:
            raise ConfigInvalid(f"unknown conflict request {request_id!r}")
        answer = OwnerAnswer(decision=decision.upper())
        if conflict is not None and decision.upper() == "ALLOW":
            fingerprint = str(conflict.get("fingerprint", ""))
            if fingerprint:
                state.override_allowed[fingerprint] = answer
                state.denied_fingerprints.pop(fingerprint, None)
        elif conflict is not None and decision.upper() == "DENY":
            fingerprint = str(conflict.get("fingerprint", ""))
            state.override_allowed.pop(fingerprint, None)
            if fingerprint:
                state.denied_fingerprints[fingerprint] = answer
        self._write(state)
        return answer

    def is_denied(self, fingerprint: str) -> bool:
        return fingerprint in self.state_or_empty().denied_fingerprints

    def consume_override(self, fingerprint: str) -> bool:
        """Single-use ALLOW (SPEC §5: ALLOW -> execute with explicit override)."""
        state = self.state()
        if fingerprint in state.override_allowed:
            del state.override_allowed[fingerprint]
            self._write(state)
            return True
        return False

    # ------------------------------------------------------------ views ---
    @property
    def epoch(self) -> int:
        return self.state().epoch

    @property
    def has_full_autonomy(self) -> bool:
        state = self.state_or_empty()
        return state.grant is not None and state.grant.profile == "FULL_PC_AUTONOMY"
