"""Tests for the machine profile (environment facts injected into every task)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nomadicos.agent.machine_profile import build_profile, ensure_profile


def test_profile_contains_common_apps() -> None:
    text = build_profile(installed_apps=["chrome.exe", "msedge.exe", "notepad.exe"])
    assert "start chrome" in text
    assert "start msedge" in text
    assert "notepad" in text
    assert "PowerShell" in text
    assert "NO bash" in text


def test_profile_lists_registry_apps() -> None:
    text = build_profile(installed_apps=["chrome.exe", "spotify.exe"])
    assert "chrome.exe" in text.replace("chrome.exe", "chrome.exe")  # listed
    assert "spotify" in text


def test_ensure_profile_writes_once(tmp_path: Path) -> None:
    first = ensure_profile(tmp_path)
    second = ensure_profile(tmp_path)
    assert first == second
    assert (tmp_path / "machine-profile.md").exists()


def test_profile_injected_into_propose(tmp_path: Path) -> None:
    from nomadicos.agent.runtime import AgentRuntime
    from nomadicos.models.fake import FakeLocalModel

    profile = build_profile(installed_apps=[])
    model = FakeLocalModel("fake/planner")
    import asyncio as _a

    _a.run(model.load())
    model._responses = [json.dumps({"finished": True})]

    class _Minimal:
        _skills = None
        _machine_profile = profile
        _memory_context: list = []
        _model = model

        class _G:  # noqa: N801
            def registered_tools(self):
                return []

            def get(self, name):  # pragma: no cover
                raise KeyError(name)

        _gateway = _G()

    rt = _Minimal()
    holder: dict = {}
    orig = model.generate

    async def capture(request):
        holder["prompt"] = request.prompt
        return await orig(request)

    model.generate = capture
    asyncio.run(AgentRuntime._propose(rt, model, "open chrome", []))
    assert "start chrome" in holder["prompt"]
    assert "THIS MACHINE" in holder["prompt"]
