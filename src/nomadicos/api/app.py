"""NomadicOS owner API: FastAPI surface over the existing bricks.

Endpoints (all local-owner):

- ``GET  /api/health``                  engine + model-store health
- ``GET  /api/models``                  discovered models + engine states
- ``GET  /api/authority``               grant epoch / profile / instructions
- ``POST /api/authority/grant-full``    one-time FULL_PC_AUTONOMY grant (owner)
- ``POST /api/authority/revoke-all``    REVOKE ALL ACCESS (owner)
- ``GET  /api/authority/instructions``  explicit owner instructions
- ``POST /api/authority/instructions``  add an owner instruction
- ``DELETE /api/authority/instructions/{iid}``
- ``GET  /api/conflicts``               pending owner-conflict requests
- ``POST /api/conflicts/{rid}/answer``  owner decision ALLOW|DENY
- ``POST /api/goals``                   launch a goal run (background task)
- ``GET  /api/runs``                    in-flight + durable tasks
- ``GET  /api/runs/{task_id}``          one run's current summary
- ``POST /api/runs/{task_id}/resume``   owner decision for WAITING_OWNER

A WAITING_OWNER graph interrupt pauses the run (RunSummary.conflict); the
owner answers via ``/resume`` which feeds the authority resolver through
``NomadicApp.resume_owner`` — the same pathway the CLI uses.

Every endpoint except ``/api/health`` requires ``Authorization: Bearer
<owner token>``. Loopback is not an owner boundary: a command a task runs
is also a local process (SPEC §4, §56.5). The token is minted per process,
held in memory only, and never placed in the environment, which tool
processes inherit.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import secrets
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from nomadicos.authority.presence import (
    OwnerPresence,
    OwnerPresenceNotConfirmed,
    presence_from_config,
    require_owner_presence,
)
from nomadicos.contracts.core import Contract, Field
from nomadicos.evaluation.cognitive import CapabilitySuiteCase
from nomadicos.kernel.config import load_config
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.orchestration.app import NomadicApp, RunSummary
from nomadicos.registry.scanner import scan_models_dir

#: dev origin of the Vite SPA (production is same-origin: no CORS needed)
_LOCAL_UI_ORIGIN = "http://localhost:5173"

RunStateValue = Literal["running", "waiting_owner", "done", "error"]

#: bounded poll delay for the UI of background orchestration state
_RUN_WAIT_S = 120.0


# --------------------------------------------------------------- requests --


class GrantRequest(Contract):
    model_config = {"extra": "forbid"}

    source: str = Field(default="owner_web", max_length=40)


class DecisionRequest(Contract):
    """Owner decision: the ONLY values the resolver accepts (fail closed)."""

    model_config = {"extra": "forbid"}

    decision: Literal["ALLOW", "DENY"]


class InstructionRequest(Contract):
    """An explicit owner instruction; resource pattern may be empty."""

    model_config = {"extra": "forbid"}

    rule: str = Field(min_length=1, max_length=500)
    resource: str = Field(default="", max_length=200)


class GoalRequest(Contract):
    model_config = {"extra": "forbid"}

    goal_text: str = Field(min_length=1, max_length=2000)
    constraints: list[str] = Field(default_factory=list)
    predicates: list[dict[str, Any]] = Field(default_factory=list)
    task_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CapabilityBenchmarkRequest(Contract):
    model_config = {"extra": "forbid"}

    cases: list[CapabilitySuiteCase] = Field(min_length=1, max_length=32)
    model_ids: list[str] = Field(default_factory=list, max_length=32)
    benchmark_version: str = Field(default="cognitive-v1", min_length=1, max_length=40)


# ------------------------------------------------------------- helpers -----


def _summary_dict(summary: RunSummary) -> dict[str, Any]:
    payload = dataclasses.asdict(summary)
    payload["status"] = summary.status.value
    payload["waiting_owner"] = summary.waiting_owner()
    return payload


def _conflict_dict(req: Any) -> dict[str, Any]:
    payload = req.model_dump()
    payload["ask"] = req.ask()
    return payload


class _RunTracker:
    """In-flight run bookkeeping over the real NomadicApp executor pathway.

    The tracker holds NO authority and decides NO outcomes; it only records
    what the graph returned. Terminal status semantics stay in NomadicApp.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def start(self, task_id: str, driver: Any) -> None:
        if task_id in self._entries and self._entries[task_id]["state"] == "running":
            raise HTTPException(status_code=409, detail="task already running")
        self._entries[task_id] = {
            "task_id": task_id,
            "state": "running",
            "summary": None,
            "error": None,
            "started_at": time.time(),
        }
        self._locks.setdefault(task_id, asyncio.Lock())
        self._tasks[task_id] = asyncio.create_task(self._drive(task_id, driver))

    async def _drive(self, task_id: str, driver: Any) -> None:
        async with self._locks[task_id]:
            entry = self._entries[task_id]
            try:
                summary = await driver()
            except Exception as exc:  # noqa: BLE001 - surfaced to the owner verbatim-ish
                entry["state"] = "error"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                return
            entry["summary"] = _summary_dict(summary)
            entry["state"] = "waiting_owner" if summary.waiting_owner() else "done"

    def state(self, task_id: str) -> dict[str, Any] | None:
        return self._entries.get(task_id)

    def locked(self, task_id: str) -> bool:
        lock = self._locks.get(task_id)
        return lock is not None and lock.locked()

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        return sorted(self._entries.items())

    async def drain(self, task_id: str, *, timeout: float = _RUN_WAIT_S) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                pass
        return self.state(task_id) or {}


# ---------------------------------------------------------------- factory --


def create_app(
    nomadic: NomadicApp | None = None,
    *,
    owner_token: str | None = None,
    presence: OwnerPresence | None = None,
) -> FastAPI:
    """Build the owner API.

    ``nomadic`` is an injection point for tests/fast host wiring; when None
    a NomadicApp is built from the owner's config on startup.
    ``owner_token`` is the bearer secret the owner presents; when None a
    fresh one is minted and exposed only as ``app.state.owner_token``.
    ``presence`` confirms the owner in person before a widening decision;
    when None it follows ``autonomy.owner_presence`` in the config.
    """

    holder: dict[str, NomadicApp] = {"app": nomadic or NomadicApp(load_config())}
    token = owner_token or secrets.token_urlsafe(32)
    owner_presence = presence or presence_from_config(holder["app"].config.autonomy.owner_presence)

    async def confirm_owner(purpose: str) -> None:
        """A token proves the request came from the owner UI; a widening
        decision also needs the owner in person (SPEC §56.5). The OS prompt
        blocks, so it runs off the event loop."""
        try:
            await asyncio.to_thread(require_owner_presence, owner_presence, purpose)
        except OwnerPresenceNotConfirmed as exc:
            raise HTTPException(status_code=403, detail=exc.message) from exc

    async def lifespan(app: FastAPI):
        yield
        if holder["app"] is not nomadic:  # only close what this factory built
            await holder["app"].aclose()

    app = FastAPI(title="NomadicOS owner API", lifespan=lifespan)
    app.state.nomadic_holder = holder
    app.state.owner_token = token

    @app.middleware("http")
    async def require_owner(request: Request, call_next: Any) -> Any:
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
            presented = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if not hmac.compare_digest(presented.encode(), expected.encode()):
                return JSONResponse({"detail": "owner token required"}, status_code=401)
        return await call_next(request)

    # added after the owner check so CORS stays outermost: preflights are
    # answered without a token and 401s still carry CORS headers
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_LOCAL_UI_ORIGIN],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    runs = _RunTracker()
    app.state.runs = runs

    def _nomadic() -> NomadicApp:
        return app.state.nomadic_holder["app"]

    # ------------------------------------------------------------- health --
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        nomadic = _nomadic()
        engines: list[dict[str, Any]] = []
        for name, engine in nomadic.engines.items():
            try:
                state = await engine.health()
                engines.append(
                    {
                        "engine": name,
                        "status": state.status.value,
                        "detail": state.detail,
                        "models": list(state.models),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - health must degrade, not fail
                engines.append(
                    {"engine": name, "status": "ERROR", "detail": str(exc)[:200], "models": []}
                )
        return {"engines": engines, "authority_epoch": nomadic.store.epoch}

    # ------------------------------------------------------------- models --
    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        nomadic = _nomadic()
        files = [
            {
                "model_id": rec.model_id,
                "source": rec.source,
                "engine": rec.engine,
                "roles": list(rec.roles),
            }
            for rec in nomadic.registry.all()
        ]
        for rec in scan_models_dir(Path(nomadic.config.inference.models_dir)):
            if rec.model_id not in {m["model_id"] for m in files}:
                files.append(
                    {
                        "model_id": rec.model_id,
                        "source": rec.source,
                        "engine": rec.engine,
                        "roles": list(rec.roles),
                    }
                )
        return {"models": files}

    # ---------------------------------------------------------- authority --
    @app.get("/api/authority")
    async def authority() -> dict[str, Any]:
        nomadic = _nomadic()
        state = nomadic.store.state()
        return {
            "epoch": state.epoch,
            "grant": state.grant.model_dump() if state.grant else None,
            "instructions": [ins.model_dump() for ins in state.instructions],
            "has_full_autonomy": nomadic.store.has_full_autonomy,
        }

    @app.post("/api/authority/grant-full")
    async def grant_full(body: GrantRequest) -> dict[str, Any]:
        nomadic = _nomadic()
        await confirm_owner("grant FULL_PC_AUTONOMY")
        nomadic.authz.grant_full(source=body.source)
        return {"granted": True, "epoch": nomadic.store.epoch}

    @app.post("/api/authority/revoke-all")
    async def revoke_all() -> dict[str, Any]:
        nomadic = _nomadic()
        return {"revoked": True, "epoch": nomadic.authz.revoke_all()}

    @app.get("/api/authority/instructions")
    async def instructions_list() -> dict[str, Any]:
        nomadic = _nomadic()
        state = nomadic.store.state()
        return {"instructions": [ins.model_dump() for ins in state.instructions]}

    @app.post("/api/authority/instructions")
    async def instructions_add(body: InstructionRequest) -> dict[str, Any]:
        nomadic = _nomadic()
        try:
            ins = nomadic.store.add_instruction(body.rule, body.resource)
        except InvalidProposal as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ins.model_dump()

    @app.delete("/api/authority/instructions/{instruction_id}")
    async def instructions_remove(instruction_id: str) -> dict[str, Any]:
        nomadic = _nomadic()
        known = {ins.id for ins in nomadic.store.state().instructions}
        if instruction_id not in known:
            raise HTTPException(status_code=404, detail="unknown instruction")
        await confirm_owner("remove an owner rule")
        nomadic.store.remove_instruction(instruction_id)
        return {"removed": instruction_id}

    # ----------------------------------------------------------- conflicts --
    @app.get("/api/conflicts")
    async def conflicts_list() -> dict[str, Any]:
        nomadic = _nomadic()
        return {"conflicts": [_conflict_dict(r) for r in nomadic.authz.pending_conflicts()]}

    @app.post("/api/conflicts/{request_id}/answer")
    async def conflicts_answer(request_id: str, body: DecisionRequest) -> dict[str, Any]:
        nomadic = _nomadic()
        if body.decision == "ALLOW":
            if request_id not in nomadic.store.state().conflicts:
                raise HTTPException(status_code=404, detail="unknown conflict request")
            await confirm_owner(f"ALLOW conflict {request_id[:40]}")
        try:
            nomadic.authz.answer_conflict(request_id, body.decision, resolver="owner")
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"answered": request_id, "decision": body.decision}

    # --------------------------------------------------------- benchmarks --
    @app.post("/api/benchmarks/capabilities")
    async def capability_benchmark(body: CapabilityBenchmarkRequest) -> dict[str, Any]:
        """Owner-local, bounded capability measurement for discovered models."""
        nomadic = _nomadic()
        await nomadic.sync_models()
        records = await nomadic.run_capability_benchmarks(
            list(body.cases),
            model_ids=body.model_ids or None,
            benchmark_version=body.benchmark_version,
            max_models=32,
        )
        return {"records": [record.model_dump(mode="json") for record in records]}

    # -------------------------------------------------------------- goals --
    @app.post("/api/goals", status_code=202)
    async def goals_run(body: GoalRequest) -> dict[str, Any]:
        nomadic = _nomadic()
        task_id = body.task_id or f"web-{int(time.time())}"
        # Same provenance as the CLI (SPEC §12): routing needs LIVE engine
        # health, and an engines-unknown registry must fail the router
        # honestly rather than route a file on disk.
        await nomadic.sync_models()
        runs.start(
            task_id,
            lambda: nomadic.run_goal(
                body.goal_text,
                constraints=body.constraints,
                predicates=body.predicates,
                task_id=task_id,
            ),
        )
        return {"task_id": task_id, "state": "running"}

    # --------------------------------------------------------------- runs --
    @app.get("/api/runs")
    async def runs_list() -> dict[str, Any]:
        nomadic = _nomadic()
        items: list[dict[str, Any]] = [dict(entry) for _tid, entry in runs.items()]
        seen = {i["task_id"] for i in items}
        for durable in nomadic.list_durable_tasks():
            if durable["task_id"] in seen:
                continue
            items.append(
                {
                    "task_id": durable["task_id"],
                    "state": "done",
                    "summary": nomadic.status(durable["task_id"]),
                    "error": None,
                    "source": durable["source"],
                }
            )
        items.sort(key=lambda i: i["task_id"])
        return {"runs": items}

    @app.get("/api/runs/{task_id}")
    async def runs_get(task_id: str) -> dict[str, Any]:
        nomadic = _nomadic()
        entry = runs.state(task_id)
        if entry is not None:
            return entry
        persisted = nomadic.status(task_id)
        if persisted is None:
            raise HTTPException(status_code=404, detail="unknown task")
        return {"task_id": task_id, "state": "done", "summary": persisted, "error": None}

    @app.post("/api/runs/{task_id}/resume", status_code=202)
    async def runs_resume(task_id: str, body: DecisionRequest) -> dict[str, Any]:
        nomadic = _nomadic()
        if runs.locked(task_id):
            raise HTTPException(status_code=409, detail="task is executing")
        entry = runs.state(task_id)
        if entry is None or entry["state"] != "waiting_owner":
            raise HTTPException(status_code=409, detail="task is not waiting for the owner")
        if body.decision == "ALLOW":
            await confirm_owner(f"ALLOW a blocked action ({task_id[:40]})")
        # resume re-enters routing after the pause: refresh engine health
        await nomadic.sync_models()
        runs.start(task_id, lambda: nomadic.resume_owner(task_id, body.decision))
        return {"task_id": task_id, "state": "running"}

    return app
