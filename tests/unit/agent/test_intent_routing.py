"""Intent routing contract.

Regression for a CONFIRMED fail-open defect: `_classify_intent` used
`text != "task"`, which let ANY non-"task" model output (empty, punctuation,
confusion, injected JSON) route an imperative to chat and report a phantom
SUCCESS. The corrected contract: only a positive 'chat' routes to chat;
everything else fails closed to the enforced task path (BP §6, §85).
"""

import asyncio

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.models.fake import FakeLocalModel


class _Stub:  # only the surface _classify_intent touches
    _skills = None
    _memory_context: list = []


def test_conversational_classifier_is_deterministic() -> None:
    # greetings/questions → chat(True); action verbs → task(False);
    # genuinely ambiguous → None (model decides).
    assert AgentRuntime._is_conversational("hello there") is True
    assert AgentRuntime._is_conversational("what is the sky") is True
    assert AgentRuntime._is_conversational("open chrome") is False
    assert AgentRuntime._is_conversational("write a file") is False
    assert AgentRuntime._is_conversational("frobnicate the widget") is None


def _classify(response: str) -> bool:
    model = FakeLocalModel("fake/x")
    asyncio.run(model.load())
    model._responses = [response]
    return asyncio.run(AgentRuntime._classify_intent(_Stub(), model, "ambiguous goal"))


def test_classification_fails_closed_to_task() -> None:
    # Only a genuine "chat" answer yields chat (True).
    assert _classify("chat") is True
    # Everything else — including a model that echoes injected JSON, rambles,
    # returns nothing, or says "task" — routes to the enforced task path.
    assert _classify("task") is False
    assert _classify("") is False
    assert _classify('{"finished": true}') is False
    assert _classify("  Chat.  ") is True
