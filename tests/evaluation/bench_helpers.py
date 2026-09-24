"""Deterministic builders for Phase 14B benchmark tests (mirrors the
orchestration test helpers: the SAME production path under a mock engine)."""

from __future__ import annotations

import json
from pathlib import Path

from nomadicos.contracts.benchmark import BenchmarkRecord
from nomadicos.contracts.model import CapabilityTag, ModelHealth, ModelRecord
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRegistry, ProductionSample


class FakeMetricStore:
    """In-memory ModelMetricStore double (unit tests only, not production)."""

    def __init__(self) -> None:
        self.benchmarks: list[BenchmarkRecord] = []
        self.production: list[ProductionSample] = []

    def save_benchmark(self, record: BenchmarkRecord) -> None:
        self.benchmarks.append(record)

    def save_production(self, sample: ProductionSample) -> None:
        self.production.append(sample)

    def load_benchmarks(self, model_id: str | None = None) -> list[BenchmarkRecord]:
        return [r for r in self.benchmarks if model_id is None or r.model_id == model_id]

    def load_production(self, model_id: str | None = None) -> list[ProductionSample]:
        return [s for s in self.production if model_id is None or s.model_id == model_id]

    def close(self) -> None:
        return None


def write_json(path: str, content: str) -> str:
    return json.dumps(
        {"tool": "filesystem", "operation": "write", "args": {"path": path, "content": content}}
    )


def make_app(
    tmp_path: Path,
    *,
    scripts: list[tuple[str, str | Exception]] | None = None,
    default_response: str = '{"finished": true}',
    grant: bool = True,
) -> NomadicApp:
    mock = MockEngine(default_response=default_response)
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
    app.mock = mock  # type: ignore[attr-defined]
    if grant:
        app.store.grant_full_pc_autonomy(source="test_owner")
    return app
