"""FastAPI wrapper around the NomadicOS Runtime (ADR-0032).

Six endpoints: POST /v1/goals, GET /v1/goals/{id}, GET /v1/events/{id},
POST /v1/memory/search, GET /v1/status, POST /v1/emergency-stop.
Goals run as background asyncio tasks; state lives in an in-memory registry.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from nomadicos.api.auth import get_or_create_token, require_token
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.core.runtime import Runtime


@dataclass
class GoalEntry:
    goal_id: str
    goal: str
    status: str = "RUNNING"
    report: dict | None = None
    idempotency_key: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass
class AppState:
    """Typed per-app state (FastAPI State is untyped)."""

    goals: dict[str, GoalEntry] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)


class GoalIn(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    user_id: str = "local-owner"
    idempotency_key: str | None = Field(default=None, max_length=128)


class MemoryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50)


def create_app(runtime: Runtime | None = None, *, token: str | None = None) -> FastAPI:
    """Build the API app around a Runtime. ``token`` overrides the token file (tests)."""
    rt = runtime or Runtime()
    app = FastAPI(
        title="NomadicOS Local API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.runtime = rt
    app.state.token = token or get_or_create_token()
    app.state.data = AppState()

    def auth(authorization: str = Header(default="")) -> None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or value != app.state.token:
            require_token(None, app.state.token)  # raises 401 uniformly

    async def _execute(goal_id: str, goal: str, user_id: str) -> None:
        entry = app.state.data.goals[goal_id]
        try:
            report = await rt.run_goal(goal, user_id=user_id)
            entry.report = report.model_dump(mode="json")
            entry.status = report.status.value
        except Exception as exc:  # noqa: BLE001 â€” truthful surface (BP Â§69)
            entry.status = TaskStatus.FAILED.value
            entry.report = {"error": f"unexpected {type(exc).__name__}: {exc}"}

    @app.post("/v1/goals", status_code=202)
    async def create_goal(body: GoalIn, _: None = Depends(auth)) -> dict:
        if body.idempotency_key and body.idempotency_key in app.state.data.by_key:
            existing = app.state.data.goals[app.state.data.by_key[body.idempotency_key]]
            return {
                "goal_id": existing.goal_id,
                "status": existing.status,
                "duplicate": True,
            }
        goal_id = str(uuid.uuid4())
        entry = GoalEntry(goal_id=goal_id, goal=body.goal, idempotency_key=body.idempotency_key)
        app.state.data.goals[goal_id] = entry
        if body.idempotency_key:
            app.state.data.by_key[body.idempotency_key] = goal_id
        entry.task = asyncio.create_task(_execute(goal_id, body.goal, body.user_id))
        return {"goal_id": goal_id, "status": entry.status, "duplicate": False}

    @app.get("/v1/goals/{goal_id}")
    async def get_goal(goal_id: str, _: None = Depends(auth)) -> dict:
        entry = app.state.data.goals.get(goal_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown goal")
        out: dict = {"goal_id": entry.goal_id, "status": entry.status, "goal": entry.goal}
        if entry.report is not None:
            out["report"] = entry.report
        return out

    @app.get("/v1/events/{goal_id}")
    async def stream_events(goal_id: str, _: None = Depends(auth)) -> StreamingResponse:
        entry = app.state.data.goals.get(goal_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown goal")

        async def events():
            last = None
            while True:
                if entry.status != last:
                    last = entry.status
                    yield f"event: status\ndata: {last}\n\n"
                if entry.report is not None:
                    import json as _json

                    yield f"event: report\ndata: {_json.dumps(entry.report)}\n\n"
                    return
                await asyncio.sleep(0.4)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/v1/memory/search")
    async def memory_search(body: MemoryIn, _: None = Depends(auth)) -> dict:
        results = await rt.memory.search(body.query, limit=body.limit)
        items = []
        for r in results:
            items.append(
                r if isinstance(r, dict) else {
                    "content": getattr(r, "content", str(r)),
                    "scope": str(getattr(r, "scope", "")),
                    "score": getattr(r, "score", None),
                }
            )
        return {"results": items, "count": len(items)}

    @app.get("/v1/status")
    async def service_status(_: None = Depends(auth)) -> dict:
        return {"status": rt.status_line()}

    @app.post("/v1/emergency-stop")
    async def stop(_: None = Depends(auth)) -> dict:
        rt.emergency_stop()
        return {"emergency_stopped": True}

    return app


def main() -> None:
    """Entry point: python -m nomadicos.api"""
    import uvicorn

    uvicorn.run(
        "nomadicos.api.app:create_app",
        host="127.0.0.1",
        port=8000,
        factory=True,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
