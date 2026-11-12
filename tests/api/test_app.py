"""Owner API brick tests: thin host over NomadicApp + authority surface.

Runs the REAL orchestration graph against a scripted MockEngine — no
validation, authorization, or verification is weakened anywhere (SPEC §51).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nomadicos.api.app import create_app
from nomadicos.authority.presence import PresenceResult, PresenceVerdict
from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp, RunSummary
from nomadicos.registry.model_registry import ModelRegistry


def write_json(path: str, content: str) -> str:
    return json.dumps(
        {"tool": "filesystem", "operation": "write", "args": {"path": path, "content": content}}
    )


def make_api_app(
    tmp_path: Path,
    *,
    scripts: list[tuple[str, str]] | None = None,
    default_response: str = '{"finished": true}',
    grant: bool = True,
    models: tuple[str, ...] = ("models/small.gguf", "models/big.gguf"),
) -> NomadicApp:
    mock = MockEngine(default_response=default_response)
    for needle, reply in scripts or []:
        mock.script(needle, reply)
    registry = ModelRegistry(EventLogger())
    for i, name in enumerate(models):
        registry.register(
            ModelRecord(
                model_id=name,
                engine="mock",
                roles=["worker", "coding"],
                capabilities=[
                    CapabilityTag.TEXT,
                    CapabilityTag.TOOL_USE,
                    CapabilityTag.CODING,
                    CapabilityTag.REASONING,
                    CapabilityTag.TESTING,
                ],
                health=ModelHealth.HEALTHY,
                source="manual",
                context_window=65536,
                params={"size_hint_b": 7.0 + i * 10},
            )
        )
    cfg = AppConfig.model_validate(
        {
            "inference": {"default_engine": "mock", "fallback_engine": "ollama"},
            "persistence": {"state_dir": str(tmp_path / "state")},
        }
    )
    app = NomadicApp(
        cfg,
        extra_engines={"mock": mock},
        registry=registry,
        workspace_root=tmp_path / "ws",
        state_dir=tmp_path / "state",
    )
    if grant:
        app.store.grant_full_pc_autonomy(source="test_owner")
    return app


def wait_terminal(client: TestClient, task_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/runs/{task_id}").json()
        if body.get("state") in ("waiting_owner", "done", "error"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {task_id} did not become terminal in {timeout}s")


OWNER_TOKEN = "test-owner-token"


class FakePresence:
    """Stands in for Windows Hello: records each prompt, answers fixed."""

    def __init__(self, verdict: PresenceVerdict = PresenceVerdict.VERIFIED) -> None:
        self.verdict = verdict
        self.prompts: list[str] = []

    def confirm(self, purpose: str) -> PresenceResult:
        self.prompts.append(purpose)
        return PresenceResult(self.verdict, "fake")


def owner_client(nomadic: NomadicApp, presence: FakePresence | None = None) -> TestClient:
    """A client presenting the owner token, as the owner UI does, with the
    owner confirmed in person unless a presence says otherwise."""
    return TestClient(
        create_app(nomadic, owner_token=OWNER_TOKEN, presence=presence or FakePresence()),
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )


@contextmanager
def api_client(nomadic: NomadicApp):
    """One long-lived request loop: background run tasks need it to survive."""
    with owner_client(nomadic) as client:
        yield client


def test_health_reports_engines_and_epoch(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path))
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    names = [e["engine"] for e in body["engines"]]
    assert "mock" in names
    assert isinstance(body["authority_epoch"], int)


def test_models_lists_registered_models(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path))
    body = client.get("/api/models").json()
    ids = [m["model_id"] for m in body["models"]]
    assert "models/small.gguf" in ids


def test_authority_roundtrip_grant_revoke(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path, grant=False))
    before = client.get("/api/authority").json()
    assert before["grant"] is None
    assert before["has_full_autonomy"] is False

    granted = client.post("/api/authority/grant-full", json={"source": "test_web"})
    assert granted.status_code == 200
    after_grant = client.get("/api/authority").json()
    assert after_grant["has_full_autonomy"] is True
    assert after_grant["grant"]["source"] == "test_web"
    assert after_grant["epoch"] == before["epoch"] + 1

    revoked = client.post("/api/authority/revoke-all")
    assert revoked.status_code == 200
    after_revoke = client.get("/api/authority").json()
    assert after_revoke["has_full_autonomy"] is False
    assert after_revoke["epoch"] == after_grant["epoch"] + 1


def test_instructions_add_list_remove(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path))
    added = client.post(
        "/api/authority/instructions", json={"rule": "Do not touch Project B", "resource": "B"}
    )
    assert added.status_code == 200
    iid = added.json()["id"]
    listed = client.get("/api/authority/instructions").json()
    assert any(ins["id"] == iid for ins in listed["instructions"])

    removed = client.delete(f"/api/authority/instructions/{iid}")
    assert removed.status_code == 200
    assert client.get("/api/authority/instructions").json()["instructions"] == []
    assert client.delete(f"/api/authority/instructions/{iid}").status_code == 404


def test_decision_validation_fails_closed(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path))
    # the request contract accepts ONLY ALLOW/DENY
    assert client.post("/api/conflicts/x/answer", json={"decision": "maybe"}).status_code == 422
    # a well-formed decision for an unknown conflict is a 404, never a silent pass
    r = client.post("/api/conflicts/nosuch/answer", json={"decision": "ALLOW"})
    assert r.status_code == 404


GOAL_OUT = "Create out.txt with hello"
PRED_OUT = [{"type": "file_exists", "path": "out.txt"}]


def test_goal_run_completes_with_goal_verification(tmp_path: Path) -> None:
    nomadic = make_api_app(
        tmp_path,
        scripts=[("out.txt", write_json("out.txt", "hello"))],
        models=("models/small.gguf",),
    )
    with api_client(nomadic) as client:
        started = client.post(
            "/api/goals", json={"goal_text": GOAL_OUT, "predicates": PRED_OUT, "task_id": "web-out"}
        )
        assert started.status_code == 202
        assert started.json() == {"task_id": "web-out", "state": "running"}

        terminal = wait_terminal(client, "web-out")
        assert terminal["state"] == "done", terminal.get("error")
        summary = terminal["summary"]
        assert summary["status"] == "SUCCESS"
        assert summary["goal_verdict"] == "PASS"
        # durable discovery sees the finished task
        assert any(t["task_id"] == "web-out" for t in client.get("/api/runs").json()["runs"])


GOAL_B = "Update the changelog inside Project B"
PRED_B = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]


def _conflict_nomadic(tmp_path: Path) -> NomadicApp:
    nomadic = make_api_app(
        tmp_path,
        scripts=[("CHANGELOG", write_json("Project B/CHANGELOG.md", "updated"))],
        models=("models/small.gguf",),
    )
    nomadic.store.add_instruction("Do not touch Project B", "Project B")
    return nomadic


def test_waiting_owner_conflict_allow_resolves(tmp_path: Path) -> None:
    with api_client(_conflict_nomadic(tmp_path)) as client:
        assert (
            client.post(
                "/api/goals",
                json={"goal_text": GOAL_B, "predicates": PRED_B, "task_id": "web-conflict"},
            ).status_code
            == 202
        )
        paused = wait_terminal(client, "web-conflict")
        assert paused["state"] == "waiting_owner"
        conflict = paused["summary"]["conflict"]
        assert "Project B" in str(conflict["conflicting_rule"])
        # paused means paused: the forbidden file was NOT written
        assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))

        resumed = client.post("/api/runs/web-conflict/resume", json={"decision": "ALLOW"})
        assert resumed.status_code == 202
        done = wait_terminal(client, "web-conflict")
        assert done["state"] == "done"
        summary = done["summary"]
        assert summary["status"] == "SUCCESS"
        assert summary["goal_verdict"] == "PASS"
        written = list((tmp_path / "ws").rglob("CHANGELOG.md"))
        assert written and written[0].read_text(encoding="utf-8") == "updated"


def test_waiting_owner_conflict_deny_blocks(tmp_path: Path) -> None:
    with api_client(_conflict_nomadic(tmp_path)) as client:
        client.post("/api/goals", json={"goal_text": GOAL_B, "predicates": PRED_B, "task_id": "d1"})
        assert wait_terminal(client, "d1")["state"] == "waiting_owner"
        resumed = client.post("/api/runs/d1/resume", json={"decision": "DENY"})
        assert resumed.status_code == 202
        done = wait_terminal(client, "d1")
        assert done["state"] == "done"
        # DENY must NOT produce a verified success
        assert done["summary"]["goal_verdict"] != "PASS"
        assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))


def test_resume_guards_fail_closed(tmp_path: Path) -> None:
    client = owner_client(make_api_app(tmp_path))
    # unknown task is never resumable
    r = client.post("/api/runs/nosuch/resume", json={"decision": "ALLOW"})
    assert r.status_code == 409
    # unknown run lookup is a 404, not a fabricated state
    assert client.get("/api/runs/nosuch").status_code == 404


async def test_tracker_transition_semantics() -> None:
    """The tracker surfaces only what the driver returned — no invented states."""
    from nomadicos.api.app import _RunTracker

    async def fail() -> dict[str, Any]:
        raise RuntimeError("boom")

    tracker = _RunTracker()
    tracker.start("t-error", fail)
    entry = await tracker.drain("t-error")
    assert entry["state"] == "error"
    assert "RuntimeError" in (entry["error"] or "")

    async def finish() -> Any:
        return RunSummary(task_id="t-done", status=TaskStatus.SUCCESS)

    tracker2 = _RunTracker()
    tracker2.start("t-done", finish)
    entry2 = await tracker2.drain("t-done")
    assert entry2["state"] == "done"


# --------------------------------------------------- owner token (§56.5) --

#: every owner-only route: loopback alone is not an owner boundary, because
#: a command a task runs is also a local process
OWNER_ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/authority", None),
    ("POST", "/api/authority/grant-full", {"source": "owner_web"}),
    ("POST", "/api/authority/revoke-all", None),
    ("POST", "/api/authority/instructions", {"rule": "r", "resource": "x"}),
    ("DELETE", "/api/authority/instructions/any-id", None),
    ("GET", "/api/conflicts", None),
    ("POST", "/api/conflicts/any-id/answer", {"decision": "ALLOW"}),
    ("POST", "/api/goals", {"goal_text": "Create a.txt containing a"}),
    ("GET", "/api/runs", None),
    ("POST", "/api/runs/any-id/resume", {"decision": "ALLOW"}),
    ("GET", "/api/models", None),
]


def test_owner_routes_refuse_requests_without_the_token(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path, grant=False)
    client = TestClient(create_app(nomadic, owner_token=OWNER_TOKEN, presence=FakePresence()))
    for method, path, body in OWNER_ROUTES:
        r = client.request(method, path, json=body)
        assert r.status_code == 401, (method, path, r.status_code)
    # nothing behind the gate ran: no grant appeared
    assert nomadic.store.has_full_autonomy is False


def test_owner_routes_refuse_a_wrong_token(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path, grant=False)
    client = TestClient(create_app(nomadic, owner_token=OWNER_TOKEN, presence=FakePresence()))
    for presented in ("Bearer wrong", OWNER_TOKEN, f"Bearer {OWNER_TOKEN}x", ""):
        r = client.post(
            "/api/authority/grant-full",
            json={"source": "owner_web"},
            headers={"Authorization": presented},
        )
        assert r.status_code == 401
    assert nomadic.store.has_full_autonomy is False


def test_task_cannot_answer_its_own_conflict_over_http(tmp_path: Path) -> None:
    """The bypass this closes: a model blocked by an owner rule answering
    its own conflict through the loopback API."""
    nomadic = make_api_app(tmp_path)
    nomadic.store.record_conflict("req-1", {"fingerprint": "fp-1"})
    anonymous = TestClient(create_app(nomadic, owner_token=OWNER_TOKEN, presence=FakePresence()))
    assert (
        anonymous.post("/api/conflicts/req-1/answer", json={"decision": "ALLOW"}).status_code == 401
    )
    state = nomadic.store.state()
    assert "req-1" in state.conflicts
    assert "fp-1" not in state.override_allowed


def test_health_stays_open_and_minted_token_is_unique(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path)
    first = create_app(nomadic, presence=FakePresence())
    second = create_app(nomadic, presence=FakePresence())
    assert first.state.owner_token != second.state.owner_token
    assert len(first.state.owner_token) >= 32
    assert TestClient(first).get("/api/health").status_code == 200


def test_cors_preflight_needs_no_token(tmp_path: Path) -> None:
    client = TestClient(
        create_app(make_api_app(tmp_path), owner_token=OWNER_TOKEN, presence=FakePresence())
    )
    r = client.options(
        "/api/authority",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200


# ----------------------------------------- owner presence (Windows Hello) --


def test_grant_needs_the_owner_in_person(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path, grant=False)
    declined = FakePresence(PresenceVerdict.DECLINED)
    r = owner_client(nomadic, declined).post("/api/authority/grant-full", json={})
    assert r.status_code == 403
    assert declined.prompts == ["grant FULL_PC_AUTONOMY"]
    assert nomadic.store.has_full_autonomy is False
    confirmed = FakePresence()
    assert owner_client(nomadic, confirmed).post("/api/authority/grant-full", json={}).is_success
    assert nomadic.store.has_full_autonomy is True


def test_allow_answer_needs_presence_but_deny_and_revoke_never_wait(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path)
    nomadic.store.record_conflict("req-1", {"fingerprint": "fp-1"})
    nomadic.store.record_conflict("req-2", {"fingerprint": "fp-2"})
    declined = FakePresence(PresenceVerdict.UNAVAILABLE)
    client = owner_client(nomadic, declined)
    r = client.post("/api/conflicts/req-1/answer", json={"decision": "ALLOW"})
    assert r.status_code == 403
    assert "fp-1" not in nomadic.store.state().override_allowed
    assert "req-1" in nomadic.store.state().conflicts
    # restricting decisions go through even with no way to confirm presence
    assert client.post("/api/conflicts/req-2/answer", json={"decision": "DENY"}).is_success
    assert client.post("/api/authority/revoke-all").is_success
    assert declined.prompts == ["ALLOW conflict req-1"]


def test_unknown_conflict_is_refused_before_any_prompt(tmp_path: Path) -> None:
    presence = FakePresence()
    client = owner_client(make_api_app(tmp_path), presence)
    r = client.post("/api/conflicts/nosuch/answer", json={"decision": "ALLOW"})
    assert r.status_code == 404
    assert presence.prompts == []


def test_removing_a_rule_needs_presence_adding_one_does_not(tmp_path: Path) -> None:
    nomadic = make_api_app(tmp_path)
    declined = FakePresence(PresenceVerdict.DECLINED)
    client = owner_client(nomadic, declined)
    added = client.post("/api/authority/instructions", json={"rule": "no B", "resource": "B"})
    assert added.is_success and declined.prompts == []
    r = client.delete(f"/api/authority/instructions/{added.json()['id']}")
    assert r.status_code == 403
    assert [i.rule for i in nomadic.store.state().instructions] == ["no B"]
