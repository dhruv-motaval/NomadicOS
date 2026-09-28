"""Structured cognitive adapter: InferenceEngine -> brain StructuredGenerator
(SPEC §9, §56.13; NomadicBrain Sprint 2 model boundary).

This adapter is the ONLY place where model invocation details for the
brain live: prompt wording, engine selection, model ids, timeouts. The
brain domain (``nomadicos.brain``) depends only on the
``StructuredGenerator`` protocol and never imports this module, so
providers can change (llama.cpp, Ollama, mock) without touching brain
contracts. The brain still validates all returned text itself — this
adapter adds no trust.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from nomadicos.inference.base import ChatMessage, GenerationRequest, InferenceEngine
from nomadicos.kernel.errors import ModelError


class _StructuredRequestLike(Protocol):
    """Structural view of a brain ``CognitiveRequest`` (no brain import)."""

    @property
    def role(self) -> Any: ...

    mission_id: str
    payload: dict[str, Any]


_DECOMPOSER_PROMPT = (
    "You are the task decomposer of an autonomous planning system. "
    "Decompose the goal into an acyclic task graph. Reply with ONLY one "
    "JSON object, no prose, no code fences, shaped exactly as: "
    '{"subgoals": [{"id": "s1", "description": "...", '
    '"success_predicate": {"type": "<predicate>", ...} | null}], '
    '"tasks": [{"id": "t1", "description": "...", "subgoal_id": "s1" | null, '
    '"depends_on": ["<task ids>"], "success_predicate": {...} | null}]}. '
    "Rules: every id matches [A-Za-z0-9][A-Za-z0-9_.-]*; dependencies "
    "reference existing task ids; no cycles; at least one task.\n\n"
    "CONTEXT:\n"
)

_ACTOR_PROMPT = (
    "You are the actor of an autonomous planning system. Propose 1 to 8 "
    "candidate next actions for the subgoal. Reply with ONLY one JSON "
    "object, no prose, no code fences, shaped exactly as: "
    '{"candidates": [{"id": "c1", "kind": "EXECUTE|OBSERVE|QUERY|'
    'EXPERIMENT|WAIT", "subgoal_id": "...", "capability": "...", '
    '"tool": "...", "arguments": {}, "preconditions": [""], '
    '"expected_effect": "...", "rationale": ""}]}. '
    "Proposals only: you cannot execute anything. "
    "Consider gathering information (OBSERVE/QUERY) when uncertain.\n\n"
    "CONTEXT:\n"
)

_MONITOR_PROMPT = (
    "You are the semantic monitor of an autonomous planning system. "
    "Evaluate each proposed candidate action against the subgoal and "
    "world facts. Reply with ONLY one JSON object, no prose, no code "
    "fences, shaped exactly as: "
    '{"verdicts": [{"candidate_id": "c1", "accepted": true|false, '
    '"reason": "..."}]}. '
    "Give an explicit verdict for EVERY candidate; a missing verdict "
    "rejects the candidate.\n\n"
    "CANDIDATES AND CONTEXT:\n"
)

_ROLE_PROMPTS: dict[str, str] = {
    "DECOMPOSER": _DECOMPOSER_PROMPT,
    "ACTOR": _ACTOR_PROMPT,
    "MONITOR": _MONITOR_PROMPT,
}


class EngineStructuredGenerator:
    """Adapts any ``InferenceEngine`` into the brain's
    ``StructuredGenerator`` protocol.

    The request's cognitive role selects the prompt template; the typed
    payload snapshot is appended as canonical JSON. Unknown roles fail
    closed.
    """

    def __init__(
        self,
        engine: InferenceEngine,
        model_id: str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = 1024,
    ) -> None:
        self._engine = engine
        self._model_id = model_id
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def generate_structured(self, request: _StructuredRequestLike) -> str:
        role = getattr(request.role, "value", str(request.role))
        template = _ROLE_PROMPTS.get(role)
        if template is None:
            raise ModelError(f"unknown cognitive role {role!r}")
        context = _json_dumps({"mission_id": request.mission_id, "payload": request.payload})
        response = await self._engine.generate(
            GenerationRequest(
                model_id=self._model_id,
                messages=[ChatMessage(role="user", content=template + context)],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        )
        return response.text


def _json_dumps(value: Any) -> str:
    """Canonical JSON encoding for prompt embedding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
