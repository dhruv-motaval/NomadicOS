"""Multi-mission concurrency (SPEC §46): independent goals run in parallel;
ONLY desktop work is serialized by an exclusive lease. Fakes only."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.errors import RateLimited, ResourceUnavailable, RetryBudgetExhausted
from nomadicos.orchestration.app import NomadicApp


def make_app(tmp_path: Path, **budget: Any) -> NomadicApp:
    cfg = AppConfig.model_validate({"budget": budget}) if budget else AppConfig()
    return NomadicApp(cfg, state_dir=tmp_path / "state", workspace_root=tmp_path / "ws")


# ------------------------------------------------------- desktop lease -----


async def test_desktop_lease_is_exclusive_reentrant_and_released(tmp_path: Path) -> None:
    app = make_app(tmp_path, desktop_lease_timeout_s=0.05)
    rt = app.runtime
    await rt.acquire_desktop("a")
    await rt.acquire_desktop("a")  # re-entrant for the holder
    with pytest.raises(ResourceUnavailable) as ei:
        await rt.acquire_desktop("b")
    assert ei.value.context["holder"] == "a"
    rt.release_desktop("b")  # a non-holder cannot release someone else's lease
    with pytest.raises(ResourceUnavailable):
        await rt.acquire_desktop("b")
    rt.release_desktop("a")
    await rt.acquire_desktop("b")
    rt.release_desktop("b")


async def test_second_desktop_task_proceeds_as_soon_as_first_finishes(tmp_path: Path) -> None:
    rt = make_app(tmp_path, desktop_lease_timeout_s=5).runtime
    await rt.acquire_desktop("a")
    waiter = asyncio.create_task(rt.acquire_desktop("b"))
    await asyncio.sleep(0.01)
    assert not waiter.done()  # blocked while a holds the keyboard/mouse
    rt.release_desktop("a")
    await asyncio.wait_for(waiter, 1)
    assert rt._desktop_owner == "b"


async def test_lease_released_when_mission_ends_or_crashes(tmp_path: Path) -> None:
    app = make_app(tmp_path, desktop_lease_timeout_s=0.05)

    class Boom:
        async def ainvoke(self, *_a: Any) -> Any:
            await app.runtime.acquire_desktop("t1")
            raise RuntimeError("mission crashed")

    app.graph = Boom()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await app._invoke({}, {}, "t1")
    await app.runtime.acquire_desktop("t2")  # lease was freed by the finally


# ------------------------------------------------------------ run_many -----


async def test_run_many_runs_independent_goals_in_parallel_bounded(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    live = peak = 0

    async def fake_run_goal(goal_text: str, **kw: Any) -> str:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return f"done:{goal_text}:{kw['task_id']}"

    app.run_goal = fake_run_goal  # type: ignore[method-assign]
    goals = [{"goal_text": f"g{i}", "task_id": f"t{i}"} for i in range(6)]
    out = await app.run_many(goals, max_concurrent=3)
    assert out == [f"done:g{i}:t{i}" for i in range(6)]  # input order kept
    assert peak == 3  # parallel, never above the cap


async def test_one_failing_goal_does_not_cancel_the_others(tmp_path: Path) -> None:
    app = make_app(tmp_path)

    async def fake_run_goal(goal_text: str, **_kw: Any) -> str:
        await asyncio.sleep(0.01)
        if goal_text == "bad":
            raise RuntimeError("boom")
        return goal_text

    app.run_goal = fake_run_goal  # type: ignore[method-assign]
    out = await app.run_many([{"goal_text": g} for g in ("a", "bad", "c")])
    assert out[0] == "a" and out[2] == "c" and isinstance(out[1], RuntimeError)


async def test_non_desktop_missions_never_wait_on_the_desktop_lease(tmp_path: Path) -> None:
    app = make_app(tmp_path, desktop_lease_timeout_s=0.05)
    await app.runtime.acquire_desktop("desktop-task")  # someone holds the desktop

    async def fake_run_goal(goal_text: str, **_kw: Any) -> str:
        return goal_text  # coding/research missions never touch the lease

    app.run_goal = fake_run_goal  # type: ignore[method-assign]
    assert await app.run_many([{"goal_text": "code"}, {"goal_text": "research"}]) == [
        "code",
        "research",
    ]


# --------------------------------------------------- cloud shared limits ---


async def test_cloud_requests_share_one_concurrency_limit_and_cool_down() -> None:
    from tests.inference.test_cloud_inference import (  # type: ignore[import-not-found]
        FakeCloud,
        greq,
        make_cfg,
        make_engine,
        or_model,
    )

    fake = FakeCloud([or_model("vendor/a:free"), or_model("vendor/b:free", ctx=65536)])
    cfg = make_cfg(max_concurrent_requests=2, max_retries=0, max_fallbacks=0)
    engine, _ = await make_engine(fake, cfg)
    provider = engine.providers["openrouter"]
    live = peak = 0
    real_chat = provider.chat

    async def slow_chat(*a: Any, **k: Any) -> Any:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return await real_chat(*a, **k)

    provider.chat = slow_chat  # type: ignore[method-assign]
    await asyncio.gather(*(engine.generate(greq("openrouter/vendor/a:free")) for _ in range(6)))
    assert peak == 2

    # a 429 puts the model on cooldown: the next request skips it without a call
    fake.script["vendor/a:free"] = [httpx.Response(429, headers={"retry-after": "1"})]
    provider.chat = real_chat  # type: ignore[method-assign]
    fake.chat_calls.clear()
    # cooldown (600s) longer than a request's whole budget (180s): waiting cannot help
    engine.cfg = make_cfg(  # type: ignore[misc]
        max_retries=1, max_fallbacks=1, rate_limit_cooldown_s=600
    )
    out = await engine.generate(greq("openrouter/vendor/a:free"))
    assert out.model_id == "openrouter/vendor/b:free"
    fake.chat_calls.clear()
    out = await engine.generate(greq("openrouter/vendor/a:free"))
    assert out.model_id == "openrouter/vendor/b:free" and fake.models_called() == ["vendor/b:free"]
    assert out.trace and "cooling down" in out.trace.fallback_reasons[0]
    # b rate-limits too: both cooling for longer than the budget -> typed RateLimited, no call
    fake.script["vendor/b:free"] = [httpx.Response(429)]
    with pytest.raises(RetryBudgetExhausted):
        await engine.generate(greq("openrouter/vendor/b:free"))
    fake.chat_calls.clear()
    with pytest.raises(RateLimited):
        await engine.generate(greq("openrouter/vendor/a:free"))
    assert fake.chat_calls == []
