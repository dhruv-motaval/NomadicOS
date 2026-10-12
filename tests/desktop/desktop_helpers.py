"""Builders for desktop e2e tests: the REAL graph + a deterministic
FakeDesktopBackend swapped in through the normal registration path."""

from __future__ import annotations

import json
from pathlib import Path

from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.desktop.backend import FakeDesktopBackend
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.tools import DesktopTool


def desktop_json(operation: str, args: dict) -> str:
    return json.dumps({"tool": "desktop", "operation": operation, "args": args})


def make_desktop_app(
    tmp_path: Path,
    backend: FakeDesktopBackend | None = None,
    *,
    scripts: list[tuple[str, str]] | None = None,
    default_response: str = '{"finished": true}',
    grant: bool = True,
) -> NomadicApp:
    """NomadicApp exactly as any host would build it, plus a deterministic
    desktop backend swapped in through the normal ToolRegistry registration.
    No production component is modified for tests."""
    mock = MockEngine(default_response='{"finished": true}')
    for needle, reply in scripts or []:
        mock.script(needle, reply)
    log = EventLogger()
    registry = ModelRegistry(log)
    registry.register(
        ModelRecord(
            model_id="models/small.gguf",
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
            params={"size_hint_b": 7.0},
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
    app.tools.register(DesktopTool(backend or FakeDesktopBackend(width=800, height=600)))
    app.mock = mock  # type: ignore[attr-defined]
    if grant:
        app.store.grant_full_pc_autonomy(source="test_owner")
    return app
