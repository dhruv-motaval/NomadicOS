"""HARDWARE §8.29/§8.30: LIVE local model through graph + Phase 8 verification.

Positive path:  model-authored proposal -> executor writes file -> the goal
verifier INDEPENDENTLY establishes completion -> SUCCESS.
Negative path: same live pipeline, but the goal predicate demands content the
action will not produce -> NOT_PASS -> the graph must never emit SUCCESS.

Ollama carries the live proof (llama-server binary not installed here).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.kernel.events import EventLogger, EventType
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry

pytestmark = pytest.mark.hardware

MODEL = "gemma3:4b"


def _app(tmp_path: Path) -> NomadicApp:
    log = EventLogger()
    registry = ModelRegistry(log)
    registry.register(
        ModelRecord(
            model_id=MODEL,
            engine="ollama",
            roles=["worker"],
            capabilities=[CapabilityTag.TEXT, CapabilityTag.TOOL_USE, CapabilityTag.REASONING],
            health=ModelHealth.HEALTHY,
            source="ollama",
        )
    )
    app = NomadicApp(
        registry=registry,
        workspace_root=tmp_path / "ws",
        state_dir=tmp_path / "state",
    )
    app.store.grant_full_pc_autonomy(source="hardware_owner")
    return app


async def _require_ollama(app: NomadicApp) -> None:
    try:
        health = await app.engines["ollama"].health()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"ollama unreachable: {exc}")
    if MODEL not in health.models:
        pytest.skip(f"{MODEL} not served locally")


async def test_live_model_goal_becomes_verified_success(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        await _require_ollama(app)
        expected = "PHASE8-LIVE"
        goal = f"Create file p8_live.txt containing exactly {expected}"
        # predicate uses contains: robust to whether a live model adds a
        # trailing newline; the verifier still reads the actual bytes itself.
        predicates = [{"type": "file_contains", "path": "p8_live.txt", "text": expected}]
        summary = None
        for attempt in range(3):
            summary = await app.run_goal(goal, predicates=predicates, task_id=f"p8live{attempt}")
            task_dirs = list((tmp_path / "ws").glob(f"p8live{attempt}"))
            if task_dirs and (task_dirs[0] / "p8_live.txt").exists():
                break
        assert summary is not None
        workspace = tmp_path / "ws" / summary.task_id
        target = workspace / "p8_live.txt"
        assert summary.executions >= 1, (
            f"live model never produced an executable IR action: {summary.failures}"
        )
        # --- THE biconditional (§8.27): SUCCESS iff independently verified ---
        if summary.status is TaskStatus.SUCCESS:
            assert summary.goal_verdict == "PASS"
            assert expected in target.read_text(encoding="utf-8")
        else:
            content_ok = target.exists() and expected in target.read_text(encoding="utf-8")
            assert not content_ok, "file IS correct but graph did not succeed? impossible"
            assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PARTIAL}
        # model attribution intact
        assert summary.model_id == MODEL
        types = [e.type for e in app.log.events(summary.task_id)]
        assert EventType.TOOL_EXECUTED in types and EventType.GOAL_VERIFIED in types
        print(
            "LIVE GOAL RESULT:",
            summary.status.value,
            summary.goal_verdict,
            summary.goal_why[:2],
        )
    finally:
        await app.aclose()


async def test_live_model_cannot_force_success_with_false_predicate(tmp_path: Path) -> None:
    """§8.30 live negative: model does REAL work; the goal demands content
    whose value exists ONLY in the verifier's predicate (a sha256) - the
    model cannot guess the preimage, so verification must refuse SUCCESS."""
    import hashlib

    app = _app(tmp_path)
    demanded = "IMPOSSIBLE-PHASE8-SENTINEL"
    try:
        await _require_ollama(app)
        goal = "Create file p8_wrong.txt containing exactly the text P8-ACTUAL-NOT-THE-DEMANDED"
        predicates = [
            {
                "type": "file_sha256",
                "path": "p8_wrong.txt",
                "sha256": hashlib.sha256(demanded.encode()).hexdigest(),
            }
        ]
        summary = None
        for attempt in range(2):  # need at least one real executed action
            summary = await app.run_goal(goal, predicates=predicates, task_id=f"p8wrong{attempt}")
            if summary.executions >= 1:
                break
        assert summary is not None and summary.executions >= 1, (
            f"negative-path requires a real executed action: {summary and summary.failures}"
        )
        assert summary.status is not TaskStatus.SUCCESS
        assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PARTIAL}
        goal_events = [
            e for e in app.log.events(summary.task_id) if e.type is EventType.GOAL_VERIFIED
        ]
        step_failures = [
            e
            for e in app.log.events(summary.task_id)
            if e.type is EventType.VERIFICATION_RESULT and e.result == "NOT_PASS"
        ]
        assert goal_events or step_failures, "verification must have exercised the live work"
        assert all(e.result != "PASS" for e in goal_events)
        assert summary.goal_verdict in {"NOT_PASS", None}
        written = list((tmp_path / "ws").rglob("p8_wrong.txt"))
        for f in written:  # the model did write - just not the demanded bytes
            assert (
                hashlib.sha256(f.read_bytes()).hexdigest()
                != hashlib.sha256(demanded.encode()).hexdigest()
            )
        print("LIVE NEGATIVE RESULT:", summary.status.value, summary.goal_verdict)
    finally:
        await app.aclose()
