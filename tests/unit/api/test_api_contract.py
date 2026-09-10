"""Contract tests for the local API (ADR-0032).

Runs against a FakeRuntime — no models, no PostgreSQL, no network.
Every test proves the six endpoints behave per the contract: auth fail-closed,
idempotent goal creation, truthful status surfacing, SSE stream termination.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from nomadicos.api.app import create_app
from nomadicos.core.lifecycle import TaskStatus


class FakeRuntime:
    """Minimal Runtime stand-in: deterministic reports, no side effects."""

    def __init__(self) -> None:
        self.stopped = False
        self.calls: list[str] = []
        self.memory = SimpleNamespace(search=self._search)

    async def run_goal(self, goal: str, *, user_id: str = "local-owner"):
        self.calls.append(goal)
        await asyncio.sleep(0.01)
        return SimpleNamespace(
            status=TaskStatus.SUCCESS,
            model_dump=lambda mode="dict": {
                "goal": goal, "status": "SUCCESS", "completed": ["fake"],
            },
        )

    def emergency_stop(self) -> None:
        self.stopped = True

    def status_line(self) -> str:
        return "NomadicOS | fake | persistence=memory"

    async def _search(self, query: str, *, limit: int = 5):
        return [{"content": f"hit for {query}", "scope": "USER", "score": 0.9}][:limit]


@pytest.fixture()
def client():
    # Context-manager form keeps ONE event-loop portal alive across requests —
    # background goal tasks created during POST must be able to finish while
    # later requests poll them (without this they die with the request loop).
    app = create_app(runtime=FakeRuntime(), token="test-token-123")
    with TestClient(app) as c:
        yield c


def _h(client) -> dict:
    return {"Authorization": f"Bearer {client.app.state.token}"}


def test_auth_fails_closed(client):
    # missing token
    r = client.get("/v1/status")
    assert r.status_code == 401
    # wrong token
    r = client.get("/v1/status", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_status_ok(client):
    r = client.get("/v1/status", headers=_h(client))
    assert r.status_code == 200
    assert "NomadicOS" in r.json()["status"]


def test_goal_lifecycle(client):
    h = _h(client)
    r = client.post("/v1/goals", json={"goal": "write hello"}, headers=h)
    assert r.status_code == 202
    goal_id = r.json()["goal_id"]
    assert r.json()["status"] in ("RUNNING", "SUCCESS")
    # poll until finished
    for _ in range(50):
        r = client.get(f"/v1/goals/{goal_id}", headers=h)
        if r.json()["status"] not in ("RUNNING",):
            break
        import time

        time.sleep(0.02)
    body = r.json()
    assert body["status"] == "SUCCESS"
    assert body["report"]["goal"] == "write hello"


def test_idempotency_key_returns_same_goal(client):
    h = _h(client)
    key = "retry-abc-123"
    r1 = client.post(
        "/v1/goals", json={"goal": "task one", "idempotency_key": key}, headers=h
    )
    r2 = client.post(
        "/v1/goals", json={"goal": "task one", "idempotency_key": key}, headers=h
    )
    assert r1.json()["goal_id"] == r2.json()["goal_id"]
    assert r2.json()["duplicate"] is True


def test_unknown_goal_404(client):
    r = client.get("/v1/goals/does-not-exist", headers=_h(client))
    assert r.status_code == 404


def test_memory_search(client):
    r = client.post(
        "/v1/memory/search", json={"query": "postgres", "limit": 3}, headers=_h(client)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert "postgres" in body["results"][0]["content"]


def test_emergency_stop(client):
    r = client.post("/v1/emergency-stop", headers=_h(client))
    assert r.status_code == 200
    assert client.app.state.runtime.stopped is True


def test_sse_stream_terminates_with_report(client):
    h = _h(client)
    goal_id = client.post("/v1/goals", json={"goal": "sse test"}, headers=h).json()["goal_id"]
    with client.stream(
        "GET", f"/v1/events/{goal_id}", headers=h
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes()).decode()
    assert "event: report" in body
    assert "SUCCESS" in body


def test_goal_validation(client):
    # empty goal rejected by schema
    r = client.post("/v1/goals", json={"goal": ""}, headers=_h(client))
    assert r.status_code == 422
