"""Focused regression coverage: the CLI ``run`` path must synchronize the
model registry (NomadicApp.sync_models) BEFORE goal execution — the root
cause of the routing-exhausted bug was that ``run`` skipped model
discovery while the ``code`` command already called it. The fake app
records the call order; no real engines are constructed."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from nomadicos.cli.app import app as cli_typer_app
from nomadicos.contracts.core import TaskStatus

runner = CliRunner()


class _FakeSummary:
    status = TaskStatus.SUCCESS
    outcome_note = ""
    goal_verdict = None
    goal_verifier = None
    goal_why: list[str] = []
    critic_iterations: list[dict[str, Any]] = []
    task_id = "task_1"
    model_id = "gemma3:4b"
    executions = 1
    recoveries = 0
    last_observations: list[str] = []
    failures: list[dict[str, Any]] = []

    def waiting_owner(self) -> bool:
        return False


class _FakeApp:
    """Records the CLI drive call order without any real engines."""

    calls: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _FakeApp.calls = []

    async def sync_models(self) -> None:
        _FakeApp.calls.append("sync_models")

    async def run_goal(self, goal: str, *, constraints: list[str] | None = None, **kw: Any):
        _FakeApp.calls.append(f"run_goal:{goal}")
        return _FakeSummary()

    async def resume_owner(self, task_id: str, answer: str):
        return _FakeSummary()

    async def aclose(self) -> None:
        _FakeApp.calls.append("aclose")


@pytest.fixture()
def fake_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "nomadicos.orchestration.app.NomadicApp",
        _FakeApp,
    )
    return _FakeApp


def test_run_path_synchronizes_models_before_goal(fake_app) -> None:
    result = runner.invoke(cli_typer_app, ["run", "Create test.txt containing NOMADICOS_TEST"])
    assert result.exit_code == 0, result.output
    # model discovery ran BEFORE goal execution: the run path synchronizes
    # the registry exactly once (no second discovery mechanism)
    assert fake_app.calls[0] == "sync_models"
    assert fake_app.calls[1] == "run_goal:Create test.txt containing NOMADICOS_TEST"
    assert fake_app.calls.count("sync_models") == 1
    assert "STATUS: SUCCESS" in result.output


def test_run_path_closes_app_after_drive(fake_app) -> None:
    result = runner.invoke(cli_typer_app, ["run", "goal text"])
    assert result.exit_code == 0, result.output
    # the app closes after the drive (resource hygiene preserved)
    assert fake_app.calls[-1] == "aclose"
