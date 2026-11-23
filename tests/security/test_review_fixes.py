"""Regression tests for the 2026-11 architecture review fixes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nomadicos.action_ir.validation import _resource_hint
from nomadicos.authority import AuthorityStore, CapabilityPolicy
from nomadicos.authority.policy import PolicyDecision
from nomadicos.contracts.action import CapabilityRef
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.kernel.errors import ModelError
from orchestration.helpers import make_app, write_json

MARK = "Propose exactly one next action"


def granted(tmp_path: Path) -> AuthorityStore:
    store = AuthorityStore(tmp_path / "authority.json")
    store.grant_full_pc_autonomy(source="test")
    return store


def decide(policy: CapabilityPolicy, capability: str, resource: str = "") -> PolicyDecision:
    return policy.evaluate(CapabilityRef(capability=capability, resource=resource)).decision


# ------------------------------------------------------------- policy -----


def test_two_specific_patterns_in_one_namespace_both_apply(tmp_path: Path) -> None:
    policy = CapabilityPolicy(
        granted(tmp_path), granted_patterns=["filesystem.read", "filesystem.write"]
    )
    assert decide(policy, "filesystem.write") is PolicyDecision.ALLOW
    assert decide(policy, "filesystem.read") is PolicyDecision.ALLOW
    assert decide(policy, "filesystem.delete") is PolicyDecision.DENY


def test_owner_rule_cannot_widen_the_capability_ceiling(tmp_path: Path) -> None:
    store = granted(tmp_path)
    store.add_instruction("do not touch Project B", "project b")
    policy = CapabilityPolicy(store, granted_patterns=["filesystem.*"])
    # terminal is outside the ceiling: denied outright, the owner is never asked
    assert decide(policy, "terminal.execute", "C:/Project B/run.bat") is PolicyDecision.DENY
    assert decide(policy, "filesystem.write", "C:/Project B/x") is PolicyDecision.ASK_OWNER


def test_owner_rules_see_every_target_of_an_action() -> None:
    hint = _resource_hint({"command": "cmd", "args": ["/c", "del", "C:\\Project B\\n.txt"]})
    assert "Project B" in hint
    assert "Project B" in _resource_hint({"source": "a.txt", "target": "C:/Project B/a.txt"})
    assert _resource_hint({"path": "a.txt", "content": "ignored"}) == "a.txt"


# -------------------------------------------------------------- store -----


def test_concurrent_store_writes_never_lose_a_revoke(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    AuthorityStore(path).grant_full_pc_autonomy()
    stop = threading.Event()

    def churn() -> None:
        store = AuthorityStore(path)
        n = 0
        while not stop.is_set():
            store.record_conflict(f"req{n}", {"fingerprint": "f"})
            n += 1

    worker = threading.Thread(target=churn)
    worker.start()
    try:
        for _ in range(20):
            AuthorityStore(path).revoke_all()
    finally:
        stop.set()
        worker.join()
    final = AuthorityStore(path).state()
    assert final.grant is None  # a racing write must not restore the grant
    assert final.epoch == 1 + 1 + 20  # initial + grant + 20 revokes, never rolled back


# -------------------------------------------------------------- graph -----


async def test_critic_unavailable_without_predicates_ends_partial_not_crash(tmp_path: Path) -> None:
    from nomadicos.agents.critic import ModelCritic

    app = make_app(tmp_path, scripts=[("Evaluate now", ModelError("critic dead"))])
    app.registry.register(
        ModelRecord(
            model_id="models/critic1.gguf",
            engine="mock",
            roles=["critic", "reasoning"],
            capabilities=[CapabilityTag.TEXT],
            health=ModelHealth.HEALTHY,
            source="manual",
            params={"size_hint_b": 26},
        )
    )
    app.runtime.critic = ModelCritic(app.registry, app.selector, app.runtime.engine_for)
    summary = await app.run_goal("Tidy up the project notes", task_id="crit")
    assert summary.status is not TaskStatus.SUCCESS
    await app.aclose()


async def test_brain_planner_with_no_routable_model_fails_like_the_legacy_path(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, models=(), brain_planner=True)
    summary = await app.run_goal(
        "Create hello.txt", task_id="nomodel", predicates=[{"type": "file_exists", "path": "h.txt"}]
    )
    assert summary.status is TaskStatus.FAILED
    await app.aclose()


async def test_reusing_a_task_id_is_refused(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    await app.run_goal("Say hi", task_id="same")
    with pytest.raises(ValueError, match="already has a run"):
        await app.run_goal("Say hi again", task_id="same")
    await app.aclose()


# -------------------------------------------------------------- audit -----


async def test_events_are_written_to_a_durable_audit_file(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=[(MARK, [write_json("a.txt", "x")])])
    await app.run_goal("Write a.txt", task_id="aud")
    app.authz.revoke_all()
    await app.aclose()
    lines = [
        json.loads(line)
        for line in (tmp_path / "state" / "audit-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    types = {e["type"] for e in lines}
    assert {"TASK_CREATED", "AUTHORIZATION_GRANTED", "TOOL_EXECUTED", "PERMISSION_REVOKED"} <= types
