"""Tests for machine-local skill learning (caveman/ponytail notes)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nomadicos.agent.skills import SkillStore


def test_save_caps_lines_and_slugifies(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    content = "\n".join(f"line {i}" for i in range(20))
    path = store.save("Open chrome and play music", content)
    assert path.exists()
    assert path.name.endswith(".md")
    saved_lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(saved_lines) <= 10  # token-cheap by design


def test_find_matches_by_word_overlap(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.save("open chrome browser", "chrome: use start chrome")
    store.save("unrelated task about gardening", "gardens need water")
    hits = store.find("can you open chrome for me")
    assert len(hits) == 1
    assert "start chrome" in hits[0]


def test_find_requires_min_overlap(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.save("open chrome", "chrome note")
    assert store.find("write a poem about the sea") == []


def test_find_best_match_first(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.save("open chrome now", "weak match note")
    store.save("open chrome and play music video", "strong match note")
    hits = store.find("open chrome and play music video please")
    assert hits[0] == "strong match note"


class _MinimalRuntime:
    """Only the surface _learn_skill/_propose touch — no heavy ctor."""

    def __init__(self, model, store, gateway=None):
        self._skills = store
        self._model = model
        self._gateway = gateway
        self._memory_context: list = []


def _make_gateway():
    class G:
        def registered_tools(self):
            return []

        def get(self, name):  # pragma: no cover — never called (no tools)
            raise KeyError(name)

    return G()


def _loaded(*model: object) -> None:
    """FakeLocalModel must be loaded before generate()."""
    for m in model:
        import asyncio as _a

        _a.run(m.load())  # type: ignore[attr-defined]


def test_learning_loop_writes_skill_on_failure(tmp_path: Path) -> None:
    from nomadicos.agent.runtime import AgentRuntime
    from nomadicos.models.fake import FakeLocalModel

    store = SkillStore(tmp_path / "skills")
    model = FakeLocalModel("fake/planner")
    _loaded(model)
    model._responses = ["chrome: not in PATH\nuse: start chrome\nwindows: verified"]
    rt = _MinimalRuntime(model, store)

    asyncio.run(
        AgentRuntime._learn_skill(rt, model, "open chrome", ["terminal: command not found"])
    )
    saved = list((tmp_path / "skills").glob("*.md"))
    assert len(saved) == 1
    assert "start chrome" in saved[0].read_text(encoding="utf-8")


def test_learning_loop_skips_when_model_says_skip(tmp_path: Path) -> None:
    from nomadicos.agent.runtime import AgentRuntime
    from nomadicos.models.fake import FakeLocalModel

    store = SkillStore(tmp_path / "skills")
    model = FakeLocalModel("fake/planner")
    _loaded(model)
    model._responses = ["SKIP"]
    rt = _MinimalRuntime(model, store)

    asyncio.run(AgentRuntime._learn_skill(rt, model, "something", ["no info"]))
    assert list((tmp_path / "skills").glob("*.md")) == []


def test_skill_injection_into_propose(tmp_path: Path) -> None:
    """Matching skill notes appear in the proposal prompt."""
    from nomadicos.agent.runtime import AgentRuntime
    from nomadicos.models.fake import FakeLocalModel

    store = SkillStore(tmp_path / "skills")
    store.save("open chrome", "chrome: use start chrome")
    model = FakeLocalModel("fake/planner")
    _loaded(model)
    model._responses = [json.dumps({"finished": True})]
    rt = _MinimalRuntime(model, store, _make_gateway())

    holder: dict = {}
    orig_generate = model.generate

    async def capture(request):
        holder["prompt"] = request.prompt
        return await orig_generate(request)

    model.generate = capture
    asyncio.run(AgentRuntime._propose(rt, model, "can you open chrome", []))
    assert "start chrome" in holder["prompt"]
    assert "MACHINE FACTS" in holder["prompt"]
