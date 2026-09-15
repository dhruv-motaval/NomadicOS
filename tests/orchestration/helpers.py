"""Shared deterministic orchestration builders for Phase 7 tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry


def write_json(path: str, content: str) -> str:
    return json.dumps(
        {"tool": "filesystem", "operation": "write", "args": {"path": path, "content": content}}
    )


def term_json(command: str, *args: str) -> str:
    return json.dumps(
        {
            "tool": "terminal",
            "operation": "execute",
            "args": {"command": command, "args": list(args)},
        }
    )


def make_app(
    tmp_path: Path,
    *,
    scripts: list[tuple[str, str | Exception]] | None = None,
    default_response: str = '{"finished": true}',
    fail_models: tuple[str, ...] = (),
    grant: bool = True,
    models: tuple[str, ...] = ("models/small.gguf", "models/big.gguf"),
    config_over: dict[str, Any] | None = None,
    small_size: float = 7.0,
) -> NomadicApp:
    mock = MockEngine(default_response=default_response)
    for needle, reply in scripts or []:
        mock.script(needle, reply)
    if fail_models:
        mock.fail_for(*fail_models)
    log = EventLogger()
    registry = ModelRegistry(log)
    for i, name in enumerate(models):
        registry.register(
            ModelRecord(
                model_id=name,
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
                params={"size_hint_b": small_size + i * 10},
            )
        )
    cfg = AppConfig.model_validate(
        {
            "inference": {"default_engine": "mock", "fallback_engine": "ollama"},
            "persistence": {"state_dir": str(tmp_path / "state")},
            **(config_over or {}),
        }
    )
    app = NomadicApp(
        cfg,
        extra_engines={"mock": mock},
        registry=registry,
        workspace_root=tmp_path / "ws",
        state_dir=tmp_path / "state",
    )
    app.mock = mock  # type: ignore[attr-defined]
    if grant:
        app.store.grant_full_pc_autonomy(source="test_owner")
    return app
