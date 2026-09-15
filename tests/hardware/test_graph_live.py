"""HARDWARE: live local model drives the Phase 7 graph end-to-end.

Same pipeline as tests/hardware/test_real_model_path.py (phase 6) but
orchestrated: intake -> classify -> plan -> select -> PROPOSE (live model)
-> validate -> authorize -> execute -> observe -> verify(step/goal).

Ollama carries the live proof for now: llama-server binary is not yet on
PATH on this machine; both engines satisfy one contract for the graph.
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
GOAL = "Create file graph_live.txt containing PHASE7-LIVE"
PRED = [{"type": "file_exists", "path": "graph_live.txt"}]


async def test_live_model_through_langgraph_production_path(tmp_path: Path) -> None:
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
    try:
        health = await app.engines["ollama"].health()
        if MODEL not in health.models:
            pytest.skip(f"{MODEL} not served by local ollama")
        app.store.grant_full_pc_autonomy(source="hardware_owner")
        # small, proven model: retry up to 3 fresh task threads (bounded)
        last = None
        for attempt in range(3):
            summary = await app.run_goal(GOAL, predicates=PRED, task_id=f"livegraph{attempt}")
            last = summary
            if summary.executions >= 1:
                break
        assert last is not None
        files = {
            p.name: p.read_text(encoding="utf-8")
            for p in (tmp_path / "ws").rglob("*")
            if p.is_file()
        }
        print("LIVE GRAPH FILES:", files)
        assert last.executions >= 1, (
            f"live model never produced an executable IR action: {last.failures}"
        )
        assert last.model_id == MODEL
        # completion integrity (Phase 8 default verifier active): SUCCESS here
        # must be backed by real file evidence; otherwise PARTIAL/FAILED.
        assert last.status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL, TaskStatus.FAILED}
        if last.status is TaskStatus.SUCCESS:
            assert "graph_live.txt" in " ".join(files.keys())
            assert files.get("graph_live.txt", "").strip() != ""
        assert last.goal_verdict in {"PASS", "NOT_PASS", "NOT_VERIFIED", None}
        if last.status is TaskStatus.SUCCESS:
            assert last.goal_verdict == "PASS"
        types = [e.type for e in app.log.events(last.task_id)]
        assert EventType.MODEL_SELECTED in types
        assert EventType.ACTION_VALIDATED in types
        assert EventType.AUTHORIZATION_GRANTED in types
        assert EventType.TOOL_EXECUTED in types
    finally:
        await app.aclose()
