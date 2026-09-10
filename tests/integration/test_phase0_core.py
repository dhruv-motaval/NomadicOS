"""Integration smoke: core components wired together (Phase 0 gate)."""

from nomadicos.audit.base import AuditEvent, AuditEventCategory
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.invariants import validate_policy_against_invariants
from nomadicos.constitution.policy_schema import OwnerPolicy, PolicyDocument
from nomadicos.core.config import load_config
from nomadicos.core.events import EventBus, TraceContext
from nomadicos.core.lifecycle import Lifecycle, TaskState, TaskStatus


def test_config_loads_from_shipped_default() -> None:
    config = load_config()  # no file: pure defaults must validate
    assert config.environment == "development"
    assert config.paths.vector_dir.name == "vector"


async def test_lifecycle_startup_shutdown_ordering() -> None:
    bus = EventBus()
    events: list[str] = []

    def recorder(label: str):
        async def handler(event) -> None:
            events.append(label)

        return handler

    bus.subscribe("STARTING", recorder("STARTING"))
    bus.subscribe("READY", recorder("READY"))
    bus.subscribe("STOPPED", recorder("STOPPED"))

    lifecycle = Lifecycle(bus)
    calls: list[str] = []

    async def init_a() -> None:
        calls.append("init:security")

    async def init_b() -> None:
        calls.append("init:postgres")

    async def stop_b() -> None:
        calls.append("stop:postgres")

    async def stop_a() -> None:
        calls.append("stop:security")

    lifecycle.register("security", init_a, stop_a)
    lifecycle.register("postgres", init_b, stop_b)

    await lifecycle.startup()
    assert lifecycle.state == "READY"
    # BP §234: security initializes before other subsystems
    assert calls[:2] == ["init:security", "init:postgres"]

    await lifecycle.shutdown()
    assert lifecycle.state == "STOPPED"
    assert "STOPPED" in events
    # reverse-order shutdown
    assert calls[-2:] == ["stop:postgres", "stop:security"]


async def test_emergency_stop_is_out_of_band() -> None:
    lifecycle = Lifecycle(EventBus())
    await lifecycle.startup()
    lifecycle.emergency_stop()
    await __import__("asyncio").sleep(0)  # let the bus event flush
    assert lifecycle.emergency_stopped is True
    lifecycle.reset_emergency_stop()
    assert lifecycle.emergency_stopped is False


def test_task_state_machine_transitions() -> None:
    state = TaskState(task_id="t-1")
    state.transition(TaskStatus.RUNNING)
    state.transition(TaskStatus.VERIFYING)
    state.transition(TaskStatus.SUCCESS)
    with __import__("pytest").raises(Exception):
        state.transition(TaskStatus.RUNNING)  # terminal state


async def test_audit_and_events_correlate_by_trace() -> None:
    sink = FakeAuditSink()
    bus = EventBus()
    ctx = TraceContext(request_id="req-9", user_id="u", session_id="s", task_id="t")

    async def handler(event) -> None:
        await sink.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_REQUESTED,
                task_id=event.context.task_id,
                subject=event.payload.get("tool"),
            )
        )

    bus.subscribe("TOOL_REQUESTED", handler)
    await bus.emit("TOOL_REQUESTED", ctx, {"tool": "filesystem.read"})
    assert sink.events[0].task_id == "t"
    assert sink.events[0].category is AuditEventCategory.TOOL_REQUESTED


async def _audit_forwarder(sink):
    async def handler(event) -> None:
        await sink.append(
            AuditEvent(
                category=AuditEventCategory.TOOL_REQUESTED,
                task_id=event.context.task_id,
                subject=event.payload.get("tool"),
            )
        )

    return handler


def _capture(store: list[str], label: str):
    async def handler(event) -> None:
        store.append(label)

    return handler


def test_policy_invariant_validation_integrated() -> None:
    # owner policy → invariant walk → no violation for a sane policy
    owner = OwnerPolicy(autonomy_level="assisted")
    document = PolicyDocument(version="1.0.0", owner=owner)
    validate_policy_against_invariants(document.model_dump())
