"""Phase 5: authority separation matrix. Nothing but the owner grants power."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nomadicos.authority import (
    AuthorityStore,
    AuthorizationService,
    CapabilityPolicy,
    OwnerConflictRequest,
)
from nomadicos.contracts.action import (
    ActionProposal,
    AuthorizationGrant,
    AuthorizedAction,
    CapabilityRef,
)
from nomadicos.kernel.errors import AuthorizationDenied, ConfigInvalid
from nomadicos.kernel.events import EventLogger, EventType

PATTERNS = ["filesystem.*", "terminal.*", "git.*", "process.*", "desktop.*"]


@pytest.fixture()
def store(tmp_path: Path) -> AuthorityStore:
    return AuthorityStore(tmp_path / "state" / "authority.json")


def service(store: AuthorityStore, denied: list[str] | None = None) -> AuthorizationService:
    log = EventLogger()
    policy = CapabilityPolicy(store, granted_patterns=PATTERNS, hard_denied_resources=denied or [])
    return AuthorizationService(store, policy, log)


def cap(tool: str, op: str, resource: str = "") -> CapabilityRef:
    return CapabilityRef(capability=f"{tool}.{op}", resource=resource)


def proposal(**over) -> ActionProposal:
    base = dict(
        task_id="task_1",
        step_id="step_1",
        model_id="m1",
        tool="filesystem",
        operation="write",
        args={"path": "a.txt", "content": "x"},
    )
    base.update(over)
    return ActionProposal(**base)


# ------------------------------------------------------- grant lifecycle ---


def test_no_authority_by_default_fails_closed(tmp_path: Path) -> None:
    svc = service(AuthorityStore(tmp_path / "absent.json"))
    with pytest.raises(AuthorizationDenied, match="no owner grant"):
        svc.authorize(proposal(), cap("filesystem", "write", "a.txt"))


def test_full_pc_autonomy_grant_is_persistent(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "authority.json")
    svc = service(store)
    svc.grant_full()
    aa = svc.authorize(proposal(), cap("filesystem", "write", "a.txt"))
    assert isinstance(aa, AuthorizedAction)
    assert aa.grant.profile == "FULL_PC_AUTONOMY"
    assert aa.grant.granted_by == "policy"
    # tasks 2 and 3 inherit the same authority without re-granting (§5)
    for step in ("step_2", "step_3"):
        later = svc.authorize(proposal(step_id=step), cap("terminal", "execute", "pytest -q"))
        assert isinstance(later, AuthorizedAction)


def test_restart_preserves_grant_and_revocation(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    store = AuthorityStore(path)
    svc = service(store)
    svc.grant_full()
    # restart #1: grant survives (persistent)
    reopened = service(AuthorityStore(path))
    assert isinstance(
        reopened.authorize(proposal(), cap("filesystem", "write", "x")), AuthorizedAction
    )
    # revoke, then restart #2: DENIED state survives too (restart never re-grants)
    reopened.revoke_all()
    after_restart = service(AuthorityStore(path))
    assert after_restart._store.state().grant is None
    with pytest.raises(AuthorizationDenied):
        after_restart.authorize(proposal(), cap("filesystem", "write", "x"))


def test_corrupt_state_refuses_autonomy(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text("{ not json", encoding="utf-8")
    store = AuthorityStore(path)
    with pytest.raises(ConfigInvalid):
        store.state()  # loud corruption signal
    svc = service(store)  # authorization path treats it as NO grant
    with pytest.raises(AuthorizationDenied, match="no owner grant"):
        svc.authorize(proposal(), cap("filesystem", "write", "x"))


def test_state_schema_has_no_allow_all_escape_hatch(tmp_path: Path) -> None:
    from nomadicos.authority.store import AuthorityState

    AuthorityStore(tmp_path / "never-written.json")  # constructing grants nothing
    with pytest.raises(ValidationError):
        AuthorityState.model_validate({"version": 1, "always_allow": True})
    assert AuthorityState().grant is None
    assert AuthorityState().epoch == 1


# ------------------------------------------------------------- unknown -----


def test_unknown_capability_denied(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "a.json")
    svc = service(store)
    svc.grant_full()
    # network.* is not inside the configured profile: deny, never silent allow
    with pytest.raises(AuthorizationDenied, match="not inside active profile"):
        svc.authorize(proposal(), cap("network", "fetch", "http://evil"))
    bogus = cap("rmboss", "nuke", "anything")
    store.add_instruction("never", "")  # unused empty-resource instruction shouldn't match
    with pytest.raises(AuthorizationDenied):
        service(store).authorize(proposal(), bogus)


def test_hard_denied_resource_never_allowed(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "a.json")
    svc = service(store, denied=["secrets/"])
    svc.grant_full()
    with pytest.raises(AuthorizationDenied, match="hard-denied"):
        svc.authorize(proposal(), cap("filesystem", "read", "secrets/id_rsa"))


# --------------------------------------------------------- conflicts -------


def test_owner_conflict_cycle(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "a.json")
    svc = service(store)
    svc.grant_full()
    store.add_instruction("Do not touch Project B", "Project B")
    prop = proposal(args={"path": "src/Project B/main.py", "content": "x"})
    out = svc.authorize(prop, cap("filesystem", "write", "src/Project B/main.py"))
    assert isinstance(out, OwnerConflictRequest)  # request itself is NOT permission
    # model-crafted resolver is rejected
    with pytest.raises(AuthorizationDenied):
        svc.answer_conflict(out.id, "ALLOW", resolver="model")
    with pytest.raises(AuthorizationDenied):
        svc.answer_conflict(out.id, "ALLOW", resolver="external_web_page")
    # owner DENY -> remains blocked for this exact action, permanently (§5)
    svc.answer_conflict(out.id, "DENY", resolver="owner")
    with pytest.raises(AuthorizationDenied, match="previously denied"):
        svc.authorize(prop, cap("filesystem", "write", "src/Project B/main.py"))
    # a DIFFERENT action (new fingerprint) may ask; owner ALLOW -> executes
    # once, and the override is single-use
    again = svc.authorize(
        prop.model_copy(update={"args": {"path": "src/Project B/main.py", "content": "v2"}}),
        cap("filesystem", "write", "src/Project B/main.py"),
    )
    assert isinstance(again, OwnerConflictRequest)
    svc.answer_conflict(again.id, "ALLOW", resolver="owner")
    granted = svc.authorize(
        prop.model_copy(update={"args": {"path": "src/Project B/main.py", "content": "v2"}}),
        cap("filesystem", "write", "src/Project B/main.py"),
    )
    assert isinstance(granted, AuthorizedAction)
    assert granted.grant.granted_by == "owner"
    # after consuming the override the same action must ask again (§5)
    out3 = svc.authorize(
        prop.model_copy(update={"args": {"path": "src/Project B/main.py", "content": "v2"}}),
        cap("filesystem", "write", "src/Project B/main.py"),
    )
    assert isinstance(out3, OwnerConflictRequest)


def test_conflict_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    store = AuthorityStore(path)
    svc = service(store)
    svc.grant_full()
    store.add_instruction("Do not touch vault", "vault")
    out = svc.authorize(proposal(), cap("filesystem", "read", "vault/x"))
    assert isinstance(out, OwnerConflictRequest)
    pending = service(AuthorityStore(path)).pending_conflicts()
    assert [r.id for r in pending] == [out.id]
    assert "vault" in pending[0].ask()


# ------------------------------------------------- no self-authorization ---


def test_contract_types_make_model_grants_unconstructible() -> None:
    prop = proposal()
    # only 'owner' or 'policy' may be an author of grants
    for fraud in ("model", "gpt", "system_llm", "model+policy"):
        with pytest.raises(ValidationError):
            AuthorizationGrant(
                proposal_id=prop.id,
                fingerprint=prop.fingerprint(),
                profile="FULL_PC_AUTONOMY",
                granted_by=fraud,
                reason="i said so",
                authority_epoch=1,
            )
    # and the grant must bind to the exact proposal (no blanket authority)
    good = AuthorizationGrant(
        proposal_id=prop.id,
        fingerprint=prop.fingerprint(),
        profile="FULL_PC_AUTONOMY",
        granted_by="policy",
        reason="cap",
        authority_epoch=1,
    )
    other = proposal(task_id="task_2")
    with pytest.raises(ValidationError):
        AuthorizedAction(proposal=other, grant=good)


def test_revocation_invalidates_inflight_grant_by_epoch(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "a.json")
    svc = service(store)
    svc.grant_full()
    aa = svc.authorize(proposal(), cap("filesystem", "write", "a.txt"))
    assert aa.grant.authority_epoch == store.epoch
    store.revoke_all()
    # an executor MUST reject this stale artifact (tested fully in Phase 6)
    assert aa.grant.authority_epoch != store.epoch


def test_authority_events_audited(tmp_path: Path) -> None:
    store = AuthorityStore(tmp_path / "a.json")
    s = AuthorizationService(
        store, CapabilityPolicy(store, granted_patterns=PATTERNS), EventLogger()
    )
    s.grant_full()
    s.authorize(proposal(), cap("filesystem", "write", "a.txt"))
    s.revoke_all()
    with pytest.raises(AuthorizationDenied):
        s.authorize(proposal(), cap("filesystem", "write", "a.txt"))
    types = {e.type for e in s._log.events()}
    assert {
        EventType.PERMISSION_GRANTED,
        EventType.AUTHORIZATION_GRANTED,
        EventType.PERMISSION_REVOKED,
        EventType.AUTHORIZATION_DENIED,
    } <= types
