
from nomadicos.core.events import Event, EventBus, TraceContext, new_id


def test_trace_context_derives_children_without_losing_identity() -> None:
    root = TraceContext(request_id="req-1", user_id="u", session_id="s")
    task_ctx = root.child(task_id="t-1")
    run_ctx = task_ctx.child(run_id="r-1")
    step_ctx = run_ctx.child(step_id="st-1")

    assert root.request_id == "req-1"
    assert task_ctx.request_id == "req-1" and task_ctx.task_id == "t-1"
    assert run_ctx.run_id == "r-1" and run_ctx.task_id == "t-1"
    assert step_ctx.step_id == "st-1"
    assert step_ctx.session_id == "s"  # BP §399: identity flows down


def test_new_ids_are_unique() -> None:
    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000


async def test_event_bus_delivers_to_subscribers() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("TOOL_REQUESTED", handler)
    ctx = TraceContext(request_id="req-1")
    await bus.emit("TOOL_REQUESTED", ctx, {"tool": "filesystem.read"})

    assert len(received) == 1
    assert received[0].payload["tool"] == "filesystem.read"
    assert received[0].context.request_id == "req-1"


async def test_event_bus_isolates_handler_failures() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("observer must never break execution")

    async def good_handler(event: Event) -> None:
        received.append(event)

    bus.subscribe("X", bad_handler)
    bus.subscribe("X", good_handler)
    await bus.emit("X", TraceContext(), {})
    assert len(received) == 1  # execution path unaffected


async def test_event_bus_unsubscribe() -> None:
    bus = EventBus()
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe("Y", handler)
    bus.unsubscribe("Y", handler)
    await bus.emit("Y", TraceContext(), {})
    assert seen == []
