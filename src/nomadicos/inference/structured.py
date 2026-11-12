"""Structured cognitive adapter: InferenceEngine -> brain StructuredGenerator
(SPEC §9, §56.13; NomadicBrain Sprint 2 model boundary).

This adapter is the ONLY place where model invocation details for the
brain live: prompt wording, engine selection, model ids, timeouts. The
brain domain (``nomadicos.brain``) depends only on the
``StructuredGenerator`` protocol and never imports this module, so
providers can change (llama.cpp, Ollama, mock) without touching brain
contracts. The brain still validates all returned text itself — this
adapter adds no trust.

For engines that support schema-constrained generation (llama-server
``response_format`` json_schema), the adapter attaches the JSON Schema
derived from the SAME closed brain payload contract the brain validates
against — generation is constrained, validation is unchanged and
fail-closed. Engines without support ignore the field; downstream
validation is identical either way.
"""

from __future__ import annotations

import copy
import json
import re
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
    "reference existing task ids; no cycles; at least one task; together the "
    "tasks must cover EVERY part of the goal, in order (e.g. open an "
    "application AND then type into it are two tasks).\n\n"
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
    "Consider gathering information (OBSERVE/QUERY) when uncertain. "
    "When the context lists available capabilities, the capability field "
    "of each candidate MUST be exactly one of those entries (format "
    "'<tool>.<operation>') and the tool field MUST be the tool part "
    "before the dot; never invent tool or capability names. "
    "The arguments object may use ONLY the keys that capability_args lists "
    "for the chosen capability; keys marked * are required, so OMIT every "
    "other key you do not need and never fill one with an empty string. "
    "recent_observations lists what already happened: never repeat an action "
    "that already succeeded; if the subgoal is already done, act on the part of "
    "goal_objective that is still missing. "
    "Order candidates so the FIRST is the action to run NOW. Launch an "
    "application (process.launch) only if recent_observations does not "
    "show it started; once it started, look at it (desktop.ui_find or "
    "desktop.ui_tree) and then act on its controls. Looking is not progress by itself: "
    "once you have seen the control, make the change goal_objective asks for "
    "(e.g. desktop.ui_set_value to enter text). "
    "For filesystem.write use argument keys 'path' (target file path) and "
    "'content' (EXACT file content to write); for filesystem.read and "
    "filesystem.exists use 'path'; copy exact values (paths, contents) "
    "from the world facts instead of paraphrasing them.\n\n"
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

_PREDICTOR_PROMPT = (
    "You are the predictor of an autonomous planning system. Given the "
    "current world state and one proposed candidate action, predict the "
    "hypothetical world state that would result if that action were "
    "performed. Reply with ONLY one JSON object, no prose, no code "
    "fences, shaped exactly as: "
    '{"predicted_world": {"version": 0, "facts": {"key": {"key": "key", '
    '"value": "...", "kind": "PREDICTION"}}}, "confidence": 0.0, '
    '"assumptions": [""], "uncertainties": [""]}. '
    "Rules: every predicted fact kind MUST be PREDICTION (hypothetical, "
    "never FACT); at most 16 facts; at most 20 assumptions and 20 "
    "uncertainties; every list item non-empty and at most 500 chars; "
    "confidence between 0.0 and 1.0; never include authority fields "
    "(authorized/allowed/approved/bypass/skip) anywhere.\n\n"
    "STATE AND CANDIDATE:\n"
)

_EVALUATOR_PROMPT = (
    "You are the evaluator of an autonomous planning system. Given the "
    "goal, one candidate action, and its predicted future world state, "
    "estimate how useful that predicted future is for the goal. Reply "
    "with ONLY one JSON object, no prose, no code fences, shaped exactly "
    "as: "
    '{"score": 0.0, "factors": {"goal_progress": 0.0, "feasibility": 0.0, '
    '"risk": 0.0, "uncertainty": 0.0, "resource_cost": 0.0, '
    '"reversibility": 0.0}, "rationale": ""}. '
    "Rules: score and every factor between 0.0 and 1.0; higher "
    "goal_progress, feasibility, and reversibility is better; higher "
    "risk, uncertainty, and resource_cost is worse; rationale at most "
    "2000 chars; never include authority fields "
    "(authorized/allowed/approved/bypass/skip) anywhere.\n\n"
    "GOAL, CANDIDATE AND PREDICTION:\n"
)

_ROLE_PROMPTS: dict[str, str] = {
    "DECOMPOSER": _DECOMPOSER_PROMPT,
    "ACTOR": _ACTOR_PROMPT,
    "MONITOR": _MONITOR_PROMPT,
    "PREDICTOR": _PREDICTOR_PROMPT,
    "EVALUATOR": _EVALUATOR_PROMPT,
}


def _build_role_schema(role: str) -> dict[str, Any] | None:
    """JSON Schema for one cognitive role's payload, derived from the SAME
    closed contract the brain validates model output against (single
    source of truth — the adapter adds no trust and no alternate shape).

    Roles without a typed payload contract (MONITOR) return ``None`` and
    stay prompt-only, exactly as before.
    """
    if role == "ACTOR":
        from nomadicos.brain.actor import ProposalPayload

        return ProposalPayload.model_json_schema()
    if role == "DECOMPOSER":
        from nomadicos.brain.decomposer import DecompositionPayload

        return DecompositionPayload.model_json_schema()
    if role == "PREDICTOR":
        from nomadicos.brain.deliberation import PredictionPayload

        return PredictionPayload.model_json_schema()
    if role == "EVALUATOR":
        from nomadicos.brain.deliberation import EvaluationPayload

        return EvaluationPayload.model_json_schema()
    return None


_ROLE_SCHEMA_CACHE: dict[str, dict[str, Any] | None] = {}


def _role_schema(role: str) -> dict[str, Any] | None:
    """Cached ``_build_role_schema`` (schemas are immutable per role)."""
    if role not in _ROLE_SCHEMA_CACHE:
        _ROLE_SCHEMA_CACHE[role] = _build_role_schema(role)
    return _ROLE_SCHEMA_CACHE[role]


_SAFE_CAPABILITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _actor_payload_schema(payload: dict[str, Any]) -> dict[str, Any] | None:
    """ACTOR schema, optionally narrowed to the registered capability names.

    When the request snapshot lists the registered capabilities (the
    state's capability view, produced by the registry — never model
    output), the generated ``capability`` and ``tool`` fields are
    enum-constrained to those names. This is GENERATION guidance only:
    downstream validation of the proposal is unchanged and fail-closed,
    and without a capability list the schema stays free-form.
    """
    base = _role_schema("ACTOR")
    if base is None:
        return None
    capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(capabilities, list):
        return base
    valid = [c for c in capabilities if isinstance(c, str) and _SAFE_CAPABILITY.match(c)]
    if not valid:
        return base
    schema = copy.deepcopy(base)
    candidate_spec = schema.get("$defs", {}).get("CandidateSpec")
    if not isinstance(candidate_spec, dict):
        return base
    properties = candidate_spec.get("properties")
    if not isinstance(properties, dict):
        return base
    if "capability" in properties:
        properties["capability"]["enum"] = valid
    if "tool" in properties:
        properties["tool"]["enum"] = sorted({c.split(".", 1)[0] for c in valid})
    # optional fields get omitted by small models: an EXECUTE candidate then has no tool or args
    candidate_spec["required"] = sorted(
        {*candidate_spec.get("required", []), "capability", "tool", "arguments"}
    )
    return schema


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
        max_tokens: int | None = 4096,
        constrain_to_schema: bool = True,
    ) -> None:
        self._engine = engine
        self._model_id = model_id
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._constrain_to_schema = constrain_to_schema

    async def generate_structured(self, request: _StructuredRequestLike) -> str:
        role = getattr(request.role, "value", str(request.role))
        template = _ROLE_PROMPTS.get(role)
        if template is None:
            raise ModelError(f"unknown cognitive role {role!r}")
        context = _json_dumps({"mission_id": request.mission_id, "payload": request.payload})
        if role == "ACTOR" and self._constrain_to_schema:
            schema = _actor_payload_schema(request.payload)
        else:
            schema = _role_schema(role) if self._constrain_to_schema else None
        response = await self._engine.generate(
            GenerationRequest(
                model_id=self._model_id,
                messages=[ChatMessage(role="user", content=template + context)],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_schema=schema,
            )
        )
        return response.text


def _json_dumps(value: Any) -> str:
    """Canonical JSON encoding for prompt embedding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
