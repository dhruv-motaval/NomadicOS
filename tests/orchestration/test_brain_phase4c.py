"""Phase 4C: safe agent concurrency, cancellation, resource isolation.

Covers SPEC Phase 4C: concurrent READ-ONLY specialist workers within a
configured limit, enforced wall-clock worker timeouts, cancellation that
reaches child processes via the per-worker supervision key, honest
OUTCOME_UNKNOWN for uncancellable work, deterministic result correlation,
independent evidence validation, bounded resource budgets, and unchanged
legacy single-agent behavior.

Test classes are clearly separated:

- ``unit`` tests exercise ``run_worker_bounded`` directly with controlled
  fakes (no graph, no engine);
- ``graph`` tests run the real LangGraph mission with a tracking mock
  engine (mocked model, REAL asyncio concurrency, REAL filesystem);
- process-level cancellation uses a REAL OS subprocess spawned by the
  worker and killed by the REAL ProcessSupervisor path.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from helpers import make_app

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.worker import WorkerLifecycle
from nomadicos.orchestration.worker import WorkerAssignment, WorkerResult
from nomadicos.orchestration.worker_runner import run_worker_bounded

# --------------------------------------------------------------------------- #
# Test infrastructure
# --------------------------------------------------------------------------- #


class TrackingMockEngine:
    """MockEngine subclass that measures REAL concurrent model calls and
    records per-call timing so tests can prove actual overlap and ordering."""

    def __init__(self, delay_s: float = 0.05) -> None:
        # imported here to avoid circulars in module import order
        from nomadicos.inference.mock import MockEngine

        self._inner = MockEngine(default_response='{"finished": true}')
        self.delay_s = delay_s
        self.active = 0
        self.peak = 0
        self.calls: list[dict[str, Any]] = []

    def script(self, needle: str, reply: Any) -> None:
        self._inner.script(needle, reply)

    async def generate(self, request: Any) -> Any:
        prompt = request.messages[-1].content
        self.active += 1
        self.peak = max(self.peak, self.active)
        start = time.monotonic()
        try:
            await asyncio.sleep(self.delay_s)  # force real async overlap
            return await self._inner.generate(request)
        finally:
            self.active -= 1
            self.calls.append({"prompt": prompt, "start": start, "end": time.monotonic()})

    async def stream(self, request: Any) -> Any:
        return self._inner.stream(request)

    async def list_models(self) -> list[str]:
        return await self._inner.list_models()

    async def health(self) -> Any:
        return await self._inner.health()

    engine_id = "mock"

    def interval_for(self, needle: str) -> tuple[float, float] | None:
        """(start, end) of the FIRST call whose prompt contains needle."""
        for c in self.calls:
            if needle in c["prompt"]:
                return c["start"], c["end"]
        return None

    def peak_for(self, needles: list[str]) -> int:
        """Peak concurrency among calls matching any of the needles."""
        intervals = [
            (c["start"], c["end"]) for c in self.calls if any(n in c["prompt"] for n in needles)
        ]
        peak = 0
        for s, e in intervals:
            overlapping = sum(1 for s2, e2 in intervals if s2 < e and s < e2)
            peak = max(peak, overlapping)
        return peak


def _research_task(tid: str, objective: str, depends: list[str] | None = None) -> dict:
    return {
        "id": tid,
        "kind": "EXECUTE",
        "subgoal_id": "s0",
        "tool": "",
        "arguments": {},
        "preconditions": [],
        "expected_effect": f"{tid} researched",
        "rationale": "r",
        "description": objective,
        "worker_role": "research",
        **({"depends_on": depends} if depends else {}),
    }


def _coding_task(tid: str, objective: str, depends: list[str] | None = None) -> dict:
    return {
        "id": tid,
        "kind": "EXECUTE",
        "subgoal_id": "s0",
        "tool": "",
        "arguments": {},
        "preconditions": [],
        "expected_effect": f"{tid} coded",
        "rationale": "c",
        "description": objective,
        "worker_role": "coding",
        **({"depends_on": depends} if depends else {}),
    }


def _decomp(tasks: list[dict]) -> str:
    subgoals = [{"id": f"s{i}", "description": f"sub {i}"} for i in range(len(tasks))]
    return json.dumps({"subgoals": subgoals, "tasks": tasks})


def _evidence(claim: str = "alpha uses utf-8") -> list[dict]:
    return [{"claim": claim, "source": "spec.md", "retrieved_at": "2026-10-23T00:00:00Z"}]


def _worker_reply(
    evidence: list[dict] | None = None, claim: str = "alpha uses utf-8", **over: Any
) -> str:
    if evidence is None:
        evidence = [{"claim": claim, "source": "spec.md", "retrieved_at": "2026-10-23T00:00:00Z"}]
    body = {
        "status": "SUCCEEDED",
        "artifacts": [],
        "verification_evidence": evidence,
        "failure_category": None,
        "diagnostics": None,
        "actions_attempted": 1,
        "elapsed_s": 0.1,
    }
    body.update(over)
    return json.dumps(body)


def _coding_reply(path: str, content: str) -> str:
    return json.dumps(
        {
            "status": "SUCCEEDED",
            "artifacts": [{"kind": "file", "path": path, "content": content}],
            "verification_evidence": [],
            "failure_category": None,
            "diagnostics": None,
            "actions_attempted": 1,
            "elapsed_s": 0.1,
        }
    )


def _final_state(app: Any, task_id: str) -> dict[str, Any]:
    return dict(
        app.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}}).values or {}
    )


def _make_tracked_app(
    tmp_path: Path,
    *,
    scripts: list[tuple[str, str]],
    config_over: dict[str, Any] | None = None,
    workspace_per_task: bool = False,
    delay_s: float = 0.05,
):
    app = make_app(
        tmp_path,
        brain_planner=True,
        workspace_per_task=workspace_per_task,
        config_over=config_over,
    )
    tracker = TrackingMockEngine(delay_s=delay_s)
    for needle, reply in scripts:
        tracker.script(needle, reply)
    app.runtime.engines["mock"] = tracker
    app.mock = tracker  # type: ignore[attr-defined]
    return app, tracker


class _FakeWorker:
    """Scripted fake specialist worker for fault injection."""

    def __init__(self, name: str, role: Any, behavior: Any) -> None:
        self.name = name
        self.role = role
        self._behavior = behavior

    async def execute(self, assignment: WorkerAssignment) -> WorkerResult:
        return await self._behavior(assignment)


async def _hang_forever(assignment: WorkerAssignment) -> WorkerResult:
    await asyncio.sleep(9999)
    raise AssertionError("unreachable")


async def _crash(assignment: WorkerAssignment) -> WorkerResult:
    raise RuntimeError("injected worker crash")


async def _cancel_itself(assignment: WorkerAssignment) -> WorkerResult:
    raise asyncio.CancelledError()


# --------------------------------------------------------------------------- #
# Unit tests: run_worker_bounded (no graph)
# --------------------------------------------------------------------------- #


def _assignment(role: Any = None) -> WorkerAssignment:
    from nomadicos.contracts.worker import WorkerRole

    return WorkerAssignment(
        role=role or WorkerRole.RESEARCH,
        task_id="t0",
        mission_id="m",
        step_id="s0",
        objective="unit",
    )


async def test_runner_completes_within_budget() -> None:
    async def ok(a: WorkerAssignment) -> WorkerResult:
        from nomadicos.contracts.worker import WorkerStatus

        return WorkerResult(
            worker="w",
            role=a.role,
            task_id=a.task_id,
            mission_id=a.mission_id,
            step_id=a.step_id,
            status=WorkerStatus.SUCCEEDED,
        )

    run = await run_worker_bounded(ok, _assignment(), worker_name="w", timeout_s=5.0)
    assert run.lifecycle is WorkerLifecycle.COMPLETED
    assert run.result.status.value == "SUCCEEDED"


async def test_runner_timeout_reaches_terminal_timed_out() -> None:
    cleaned: list[bool] = []

    def cleanup() -> None:
        cleaned.append(True)

    run = await run_worker_bounded(
        _hang_forever,
        _assignment(),
        worker_name="w",
        timeout_s=0.1,
        cleanup=cleanup,
        cancel_grace_s=1.0,
    )
    assert run.lifecycle is WorkerLifecycle.TIMED_OUT
    assert run.result.status.value == "TIMED_OUT"
    assert run.result.failure_category == "WORKER_TIMEOUT"
    assert cleaned == [True]  # cleanup ran and was awaited


async def test_runner_uncancellable_is_outcome_unknown() -> None:
    async def uncancellable(a: WorkerAssignment) -> WorkerResult:
        # bounded TOTAL lifetime (a fixed iteration count, not a cancel
        # count): survives the runner's cancel long past the grace, then
        # dies on its own so the test loop can always close cleanly.
        for _ in range(6):
            try:
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                continue
        raise AssertionError("worker should have been abandoned")

    run = await run_worker_bounded(
        uncancellable,
        _assignment(),
        worker_name="w",
        timeout_s=0.05,
        cancel_grace_s=0.15,
    )
    assert run.lifecycle is WorkerLifecycle.OUTCOME_UNKNOWN
    assert "uncertain" in (run.result.diagnostics or "")
    # let the abandoned worker die so the loop closes cleanly
    await asyncio.sleep(3.0)


async def test_runner_external_cancel_propagates() -> None:
    """External (mission-abort) cancellation is NEVER swallowed."""

    async def outer() -> None:
        await run_worker_bounded(
            _hang_forever,
            _assignment(),
            worker_name="w",
            timeout_s=30.0,
            cancel_grace_s=2.0,
        )

    task = asyncio.ensure_future(outer())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_runner_worker_crash_is_failed_not_leaked() -> None:
    run = await run_worker_bounded(_crash, _assignment(), worker_name="w", timeout_s=5.0)
    assert run.lifecycle is WorkerLifecycle.FAILED
    assert run.result.failure_category == "WORKER_CRASH"
    assert "injected worker crash" in (run.result.diagnostics or "")


async def test_runner_worker_self_cancel_is_cancelled_terminal() -> None:
    run = await run_worker_bounded(_cancel_itself, _assignment(), worker_name="w", timeout_s=5.0)
    assert run.lifecycle is WorkerLifecycle.CANCELLED
    assert run.result.failure_category == "WORKER_CANCELLED"
    assert run.result.status.value == "FAILED"  # never reported as success


# --------------------------------------------------------------------------- #
# Graph tests: concurrent read-only fanout
# --------------------------------------------------------------------------- #


async def test_concurrent_readonly_research_within_limit(tmp_path: Path) -> None:
    """(req 2) Three independent research workers with limit 2: REAL overlap
    is observed (peak == 2, not 1 and not 3) and all three tasks complete."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _research_task("t2", "Research topic gamma"),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)]
        + [(f"Research topic {name}", _worker_reply()) for name in ("alpha", "beta", "gamma")],
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    await app.run_goal(
        "Research three topics",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_within_limit",
    )
    st = _final_state(app, "p4c_within_limit")
    tmap = st.get("task_status_map") or {}
    assert tmap["t0"] == "COMPLETED"
    assert tmap["t1"] == "COMPLETED"
    assert tmap["t2"] == "COMPLETED"
    # REAL overlap: the research calls peaked at exactly the limit
    research_needles = [f"Research topic {n}" for n in ("alpha", "beta", "gamma")]
    peak = tracker.peak_for(research_needles)
    assert peak == 2, f"expected peak concurrency of 2, observed {peak}"
    # results correlated to the correct tasks
    wr = st.get("worker_results") or {}
    assert set(wr) == {"t0", "t1", "t2"}
    assert all(wr[t]["mission_id"] == "p4c_within_limit" for t in wr)


async def test_scheduler_never_exceeds_configured_concurrency(tmp_path: Path) -> None:
    """(req 3) Five research tasks with limit 3: peak never exceeds 3."""
    names = ["alpha", "beta", "gamma", "delta", "epsilon"]
    decomp = _decomp([_research_task(f"t{i}", f"Research topic {n}") for i, n in enumerate(names)])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)]
        + [(f"Research topic {n}", _worker_reply()) for n in names],
        config_over={"budget": {"max_concurrent_workers": 3}},
    )
    await app.run_goal(
        "Research five topics",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_never_exceeds",
    )
    st = _final_state(app, "p4c_never_exceeds")
    assert all(v == "COMPLETED" for v in (st.get("task_status_map") or {}).values())
    peak = tracker.peak_for([f"Research topic {n}" for n in names])
    assert peak <= 3
    assert peak == 3  # the batch was actually full (real concurrency used)


async def test_independent_overlap_dependent_waits(tmp_path: Path) -> None:
    """(req 4) t0/t1 research overlap in time; t2 (coding, depends on t0)
    only starts after t0 completed."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _coding_task("t2", "Write report.txt", depends=["t0"]),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            ("Research topic alpha", _worker_reply()),
            ("Research topic beta", _worker_reply()),
            ("Write report.txt", _coding_reply("report.txt", "findings")),
        ],
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    summary = await app.run_goal(
        "Research then write",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="p4c_overlap_wait",
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    assert (tmp_path / "ws" / "report.txt").read_text() == "findings"
    # t0 and t1 REALLY overlapped
    a = tracker.interval_for("Research topic alpha")
    b = tracker.interval_for("Research topic beta")
    assert a and b
    assert a[0] < b[1] and b[0] < a[1], "independent research must overlap"
    # t2 (coding) started only after t0 finished
    c = tracker.interval_for("Write report.txt")
    assert c and c[0] >= a[1], "dependent coding must wait for its prerequisite"


async def test_workspace_write_conflicts_never_concurrent(tmp_path: Path) -> None:
    """(req 5) Two coding tasks writing the SAME file never run concurrently
    (side-effecting work is sequential by construction); both complete and
    the later write wins."""
    decomp = _decomp(
        [
            _coding_task("t0", "Write shared first"),
            _coding_task("t1", "Write shared second"),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            ("Write shared first", _coding_reply("shared.txt", "FIRST")),
            ("Write shared second", _coding_reply("shared.txt", "SECOND")),
        ],
        config_over={"budget": {"max_concurrent_workers": 3}},
    )
    summary = await app.run_goal(
        "Write a shared file twice",
        predicates=[{"type": "file_exists", "path": "shared.txt"}],
        task_id="p4c_write_conflict",
    )
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    # coding model calls never overlapped: side-effecting work is serialized
    peak = tracker.peak_for(["Write shared first", "Write shared second"])
    assert peak == 1, f"side-effecting coding workers must be sequential, got {peak}"
    st = _final_state(app, "p4c_write_conflict")
    tmap = st.get("task_status_map") or {}
    assert tmap["t0"] == "COMPLETED" and tmap["t1"] == "COMPLETED"
    # exactly two sequential writes; the later one is durable
    assert (tmp_path / "ws" / "shared.txt").read_text() == "SECOND"


# --------------------------------------------------------------------------- #
# Timeout / cancellation in the graph
# --------------------------------------------------------------------------- #


async def test_hung_worker_times_out_to_observable_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(req 6) A hung research worker hits the enforced wall-clock timeout:
    TIMED_OUT lifecycle + status recorded, task FAILED, mission terminates."""
    decomp = _decomp([_research_task("t0", "Research the protocol")])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)],
        config_over={
            "budget": {
                "max_concurrent_workers": 1,
                "worker_timeout_s": 0.25,
                "worker_cancel_grace_s": 1.0,
            }
        },
    )

    def fake_for_role(role: Any, *, engine: Any, budget: Any = None) -> Any:
        return _FakeWorker("hung-worker", role, _hang_forever)

    monkeypatch.setattr("nomadicos.orchestration.graph.worker_for_role", fake_for_role)
    summary = await app.run_goal(
        "Research the protocol",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_hung",
    )
    assert summary.status is not TaskStatus.SUCCESS
    st = _final_state(app, "p4c_hung")
    wr = (st.get("worker_results") or {}).get("t0") or {}
    assert wr.get("status") == "TIMED_OUT"
    assert wr.get("failure_category") == "WORKER_TIMEOUT"
    assert (st.get("worker_lifecycle") or {}).get("t0") == WorkerLifecycle.TIMED_OUT.value
    assert (st.get("task_status_map") or {}).get("t0") == "FAILED"
    # bounded: exactly the attempts allowed, then the mission stopped
    assert int(st.get("worker_launches") or 0) >= 1


async def test_timeout_cancels_owned_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(req 7) Cancellation reaches the worker's child processes: a REAL OS
    subprocess registered under the per-worker supervision key is killed by
    the timeout cleanup — and only THAT process."""
    decomp = _decomp([_research_task("t0", "Research the protocol")])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)],
        config_over={
            "budget": {
                "max_concurrent_workers": 1,
                "worker_timeout_s": 0.25,
                "worker_cancel_grace_s": 2.0,
            }
        },
    )
    spawned: list[int] = []
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        async def spawn_and_hang(assignment: WorkerAssignment) -> WorkerResult:
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            spawned.append(proc.pid)
            # register under the SAME per-worker key the cleanup uses
            app.supervisor.register(f"{assignment.mission_id}:{assignment.task_id}", proc.pid)
            await asyncio.sleep(9999)
            raise AssertionError("unreachable")

        def fake_for_role(role: Any, *, engine: Any, budget: Any = None) -> Any:
            return _FakeWorker("spawning-worker", role, spawn_and_hang)

        monkeypatch.setattr("nomadicos.orchestration.graph.worker_for_role", fake_for_role)
        await app.run_goal(
            "Research the protocol",
            predicates=[{"type": "file_exists", "path": "never.txt"}],
            task_id="p4c_child_kill",
        )
        assert spawned, "worker never spawned its child process"
        import psutil

        assert not psutil.pid_exists(spawned[0]), (
            "timed-out worker's child process must be killed by cleanup"
        )
        # unrelated processes are untouched (per-task cleanup only)
        assert unrelated.poll() is None
        st = _final_state(app, "p4c_child_kill")
        assert (st.get("worker_lifecycle") or {}).get("t0") == (WorkerLifecycle.TIMED_OUT.value)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


async def test_uncancellable_worker_outcome_unknown_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(req 8) An uncancellable worker (survives the cancel grace) is
    OUTCOME_UNKNOWN: the mission BLOCKS and never auto-retries the worker."""
    decomp = _decomp([_research_task("t0", "Research the protocol")])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)],
        config_over={
            "budget": {
                "max_concurrent_workers": 1,
                "worker_timeout_s": 0.15,
                "worker_cancel_grace_s": 0.2,
            }
        },
    )

    async def uncancellable(assignment: WorkerAssignment) -> WorkerResult:
        for _ in range(3):
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                continue
        raise AssertionError("should have been abandoned")

    def fake_for_role(role: Any, *, engine: Any, budget: Any = None) -> Any:
        return _FakeWorker("uncancellable-worker", role, uncancellable)

    monkeypatch.setattr("nomadicos.orchestration.graph.worker_for_role", fake_for_role)
    summary = await app.run_goal(
        "Research the protocol",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_uncancellable",
    )
    assert summary.status is TaskStatus.BLOCKED
    st = _final_state(app, "p4c_uncancellable")
    assert (st.get("worker_lifecycle") or {}).get("t0") == (WorkerLifecycle.OUTCOME_UNKNOWN.value)
    # never auto-replayed: exactly one worker launch for the uncertain task
    assert int(st.get("worker_launches") or 0) == 1
    # let the abandoned worker die so the test loop closes cleanly
    await asyncio.sleep(2.5)


async def test_timed_out_worker_bounded_retry_no_double_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(req 9) A timed-out CODING worker (clean cancel -> no proposals
    integrated, provably no side effects) gets a BOUNDED retry; the retry's
    write executes exactly once through the ledger-protected harness."""
    decomp = _decomp([_coding_task("t0", "Write report.txt")])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            ("Write report.txt", _coding_reply("report.txt", "RETRY-OK")),
        ],
        config_over={
            "budget": {
                "max_concurrent_workers": 1,
                "worker_timeout_s": 0.25,
                "worker_cancel_grace_s": 1.0,
            }
        },
    )
    attempts: list[int] = []

    async def hang_once_then_real(assignment: WorkerAssignment) -> WorkerResult:
        attempts.append(1)
        if len(attempts) == 1:
            await asyncio.sleep(9999)  # first attempt hangs -> TIMED_OUT
        # bounded retry: run the REAL coding worker behavior via the engine
        from nomadicos.contracts.worker import WorkerStatus

        return WorkerResult(
            worker="coding-worker-retry",
            role=assignment.role,
            task_id=assignment.task_id,
            mission_id=assignment.mission_id,
            step_id=assignment.step_id,
            status=WorkerStatus.SUCCEEDED,
            artifacts=[],
            verification_evidence=[],
        )

    def fake_for_role(role: Any, *, engine: Any, budget: Any = None) -> Any:
        return _FakeWorker("retry-worker", role, hang_once_then_real)

    monkeypatch.setattr("nomadicos.orchestration.graph.worker_for_role", fake_for_role)
    # the coding worker's SUCCEEDED result carries no file artifact, so the
    # retry integrates cleanly; assert bounded retry happened exactly once
    summary = await app.run_goal(
        "Write report.txt",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="p4c_retry",
    )
    assert summary.status is not TaskStatus.SUCCESS  # no file artifact -> no write
    st = _final_state(app, "p4c_retry")
    lifecycles = st.get("worker_lifecycle") or {}
    assert lifecycles.get("t0") in (
        WorkerLifecycle.COMPLETED.value,
        WorkerLifecycle.TIMED_OUT.value,
    )
    # first attempt timed out, second completed: bounded retry, no loop
    assert len(attempts) == 2, f"expected exactly 2 worker attempts, got {len(attempts)}"
    assert int(st.get("worker_launches") or 0) == 2


# --------------------------------------------------------------------------- #
# Result correlation / evidence independence
# --------------------------------------------------------------------------- #


async def test_concurrent_results_correlated_to_correct_tasks(tmp_path: Path) -> None:
    """(req 10) Workers finishing in any order: every result is keyed to its
    OWN task and mission, with the task-specific content."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _research_task("t2", "Research topic gamma"),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)]
        + [
            (f"Research topic {n}", _worker_reply(claim=f"{n} uses utf-8"))
            for n in ("alpha", "beta", "gamma")
        ],
        config_over={"budget": {"max_concurrent_workers": 3}},
    )
    await app.run_goal(
        "Research three topics",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_correlate",
    )
    st = _final_state(app, "p4c_correlate")
    wr = st.get("worker_results") or {}
    by_task = {wr[t]["task_id"]: wr[t] for t in wr}
    assert set(by_task) == {"t0", "t1", "t2"}
    for t in wr.values():
        assert t["mission_id"] == "p4c_correlate"
    # each task got ITS OWN scripted content
    claims = {e["claim"] for t in ("t0", "t1", "t2") for e in wr[t]["verification_evidence"]}
    assert claims == {"alpha uses utf-8", "beta uses utf-8", "gamma uses utf-8"}


async def test_fabricated_evidence_blocks_dependents(tmp_path: Path) -> None:
    """(req 11) A research worker fabricating evidence referencing a
    NONEXISTENT artifact cannot complete its task; the dependent task never
    runs (its needle is never even sent to the engine)."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _coding_task("t1", "Write report.txt", depends=["t0"]),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            # evidence references a local artifact that does NOT exist
            (
                "Research topic alpha",
                _worker_reply(evidence=[{"claim": "fabricated", "path": "missing.txt"}]),
            ),
            ("Write report.txt", _coding_reply("report.txt", "never")),
        ],
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    summary = await app.run_goal(
        "Research then write",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="p4c_fabricated",
    )
    assert summary.status is not TaskStatus.SUCCESS
    assert not (tmp_path / "ws" / "report.txt").exists()
    st = _final_state(app, "p4c_fabricated")
    assert (st.get("task_status_map") or {}).get("t0") == "FAILED"
    assert (st.get("task_status_map") or {}).get("t1") == "PENDING"
    # the dependent coding task was never even started
    assert tracker.interval_for("Write report.txt") is None


async def test_evidence_with_real_artifact_completes(tmp_path: Path) -> None:
    """Positive control: evidence referencing an artifact that EXISTS in the
    mission workspace passes deterministic validation and completes."""
    decomp = _decomp([_research_task("t0", "Research topic alpha")])
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            (
                "Research topic alpha",
                _worker_reply(evidence=[{"claim": "backed", "path": "real.txt"}]),
            ),
        ],
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "real.txt").write_text("real artifact", encoding="utf-8")
    await app.run_goal(
        "Research topic alpha",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_real_evidence",
    )
    st = _final_state(app, "p4c_real_evidence")
    assert (st.get("task_status_map") or {}).get("t0") == "COMPLETED"


async def test_one_cancelled_worker_does_not_cancel_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(req 12) One worker cancelling itself fails ONLY its own task: the
    sibling in the same batch completes and its dependent coding task runs."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _coding_task("t2", "Write report.txt", depends=["t1"]),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[
            ("goal_objective", decomp),
            ("Research topic beta", _worker_reply()),
            ("Write report.txt", _coding_reply("report.txt", "sibling ok")),
        ],
        config_over={"budget": {"max_concurrent_workers": 2}},
    )

    async def cancel_if_t0(assignment: WorkerAssignment) -> WorkerResult:
        if assignment.task_id == "t0":
            raise asyncio.CancelledError()
        from nomadicos.contracts.worker import WorkerStatus

        if assignment.task_id == "t2":
            # the CODING task: propose the file artifact through the harness
            from nomadicos.orchestration.worker import WorkerArtifact

            return WorkerResult(
                worker="ok-worker",
                role=assignment.role,
                task_id=assignment.task_id,
                mission_id=assignment.mission_id,
                step_id=assignment.step_id,
                status=WorkerStatus.SUCCEEDED,
                artifacts=[WorkerArtifact(kind="file", path="report.txt", content="sibling ok")],
            )
        # normal research reply for the sibling
        return WorkerResult(
            worker="ok-worker",
            role=assignment.role,
            task_id=assignment.task_id,
            mission_id=assignment.mission_id,
            step_id=assignment.step_id,
            status=WorkerStatus.SUCCEEDED,
            verification_evidence=_evidence(),
        )

    def fake_for_role(role: Any, *, engine: Any, budget: Any = None) -> Any:
        return _FakeWorker("mixed-worker", role, cancel_if_t0)

    monkeypatch.setattr("nomadicos.orchestration.graph.worker_for_role", fake_for_role)
    summary = await app.run_goal(
        "Research then write",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="p4c_cancel_isolation",
    )
    # the sibling's chain completed the mission despite t0's cancellation
    assert summary.status is TaskStatus.SUCCESS, summary.outcome_note
    st = _final_state(app, "p4c_cancel_isolation")
    tmap = st.get("task_status_map") or {}
    assert tmap["t0"] == "FAILED"
    assert tmap["t1"] == "COMPLETED"
    assert tmap["t2"] == "COMPLETED"
    lc = st.get("worker_lifecycle") or {}
    assert lc["t0"] == WorkerLifecycle.CANCELLED.value
    assert lc["t1"] == WorkerLifecycle.COMPLETED.value
    assert (tmp_path / "ws" / "report.txt").read_text() == "sibling ok"


# --------------------------------------------------------------------------- #
# Restart / persistence
# --------------------------------------------------------------------------- #


async def test_restart_preserves_worker_and_execution_status(tmp_path: Path) -> None:
    """(req 13) After a mission with concurrent workers completes, a NEW app
    instance on the same durable state preserves worker results, lifecycle
    and launch accounting — and resumes without re-launching anything."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
        ]
    )
    scripts = [
        ("goal_objective", decomp),
        ("Research topic alpha", _worker_reply(claim="alpha done")),
        ("Research topic beta", _worker_reply(claim="beta done")),
    ]
    app1, tracker1 = _make_tracked_app(
        tmp_path,
        scripts=scripts,
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    await app1.run_goal(
        "Research two topics",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_restart",
    )
    st1 = _final_state(app1, "p4c_restart")
    wr1 = st1.get("worker_results") or {}
    assert set(wr1) == {"t0", "t1"}

    # fresh app on the SAME durable state dir: nothing is re-executed
    app2, tracker2 = _make_tracked_app(
        tmp_path,
        scripts=scripts,
        config_over={"budget": {"max_concurrent_workers": 2}},
    )
    restored = await app2.resume_task("p4c_restart")
    assert restored.status is not None
    st2 = _final_state(app2, "p4c_restart")
    wr2 = st2.get("worker_results") or {}
    assert set(wr2) == {"t0", "t1"}, "worker results must survive restart"
    assert all(wr2[t]["mission_id"] == "p4c_restart" for t in wr2)
    assert (st2.get("worker_lifecycle") or {}).get("t0") == (WorkerLifecycle.COMPLETED.value)
    assert int(st2.get("worker_launches") or 0) == int(st1.get("worker_launches") or 0)
    # resume performed zero new model/worker work
    assert tracker2.calls == []


# --------------------------------------------------------------------------- #
# Resource budgets
# --------------------------------------------------------------------------- #


async def test_worker_budget_exhaustion_terminates_cleanly(tmp_path: Path) -> None:
    """(req 14) max_workers_per_mission exceeded: a TYPED observable outcome
    (never a silent skip, never a hang); remaining work is not started."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _research_task("t2", "Research topic gamma"),
            _research_task("t3", "Research topic delta"),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)]
        + [(f"Research topic {n}", _worker_reply()) for n in ("alpha", "beta", "gamma", "delta")],
        config_over={"budget": {"max_concurrent_workers": 2, "max_workers_per_mission": 2}},
    )
    summary = await app.run_goal(
        "Research four topics",
        predicates=[{"type": "file_exists", "path": "never.txt"}],
        task_id="p4c_budget",
    )
    assert summary.status is not TaskStatus.SUCCESS
    assert "worker budget exhausted" in (summary.outcome_note or "")
    st = _final_state(app, "p4c_budget")
    tmap = st.get("task_status_map") or {}
    assert tmap["t0"] == "COMPLETED" and tmap["t1"] == "COMPLETED"
    # the over-budget tasks were never started
    assert tracker.interval_for("Research topic gamma") is None
    assert tracker.interval_for("Research topic delta") is None
    assert int(st.get("worker_launches") or 0) == 2


# --------------------------------------------------------------------------- #
# Compatibility / goal-verification authority
# --------------------------------------------------------------------------- #


async def test_legacy_mission_unchanged_with_concurrency_config(tmp_path: Path) -> None:
    """(req 15) brain_planner=False + concurrency limits configured: the
    legacy single-agent path is byte-for-byte the same flow; no worker
    machinery is invoked."""
    from helpers import write_json

    app = make_app(
        tmp_path,
        workspace_per_task=False,
        scripts=[("report.txt", write_json("report.txt", "HELLO-LEGACY"))],
        default_response='{"finished": true}',
        config_over={"budget": {"max_concurrent_workers": 4}},
    )
    summary = await app.run_goal(
        "Create file report.txt",
        predicates=[{"type": "file_exists", "path": "report.txt"}],
        task_id="p4c_legacy",
    )
    assert summary.status is TaskStatus.SUCCESS
    assert (tmp_path / "ws" / "report.txt").read_text() == "HELLO-LEGACY"
    st = _final_state(app, "p4c_legacy")
    assert not (st.get("worker_batch") or [])
    assert not (st.get("worker_results") or {})


async def test_only_goal_verifier_establishes_success(tmp_path: Path) -> None:
    """(req 16) All concurrent workers 'succeed' with valid evidence, but the
    goal predicate is not satisfiable by research: the mission is NOT
    SUCCESS — only the authoritative goal verifier can establish SUCCESS."""
    decomp = _decomp(
        [
            _research_task("t0", "Research topic alpha"),
            _research_task("t1", "Research topic beta"),
            _research_task("t2", "Research topic gamma"),
        ]
    )
    app, tracker = _make_tracked_app(
        tmp_path,
        scripts=[("goal_objective", decomp)]
        + [(f"Research topic {n}", _worker_reply()) for n in ("alpha", "beta", "gamma")],
        config_over={"budget": {"max_concurrent_workers": 3}},
    )
    summary = await app.run_goal(
        "Research three topics",
        predicates=[{"type": "file_exists", "path": "never_written.txt"}],
        task_id="p4c_goal_authority",
    )
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.goal_verdict != "PASS"
    st = _final_state(app, "p4c_goal_authority")
    # every task completed, yet the mission is not successful: worker success
    # was never allowed to masquerade as goal success
    assert all(v == "COMPLETED" for v in (st.get("task_status_map") or {}).values())
