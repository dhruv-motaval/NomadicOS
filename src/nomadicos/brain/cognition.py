"""Model boundary for NomadicBrain (spec §1.1, §32; Sprint 2).

The brain domain depends ONLY on the ``StructuredGenerator`` protocol and
its typed ``CognitiveRequest`` — never on a concrete model, provider,
engine, HTTP client, API key, or prompt template. Everything that knows
HOW a model is invoked (prompts, engines, provider quirks) lives in the
inference adapter layer (``nomadicos.inference.structured``), which is
the only place prompt strings are allowed.

Data flow (fail closed)::

    CognitiveRequest (typed, serializable)
        -> StructuredGenerator (adapter implements this)
        -> raw JSON text
        -> strict parse + typed validation (ModelOutputRejected on any
           deviation)

The protocol returns TEXT, not objects: the brain owns typed validation
of model output, so no provider can smuggle untyped structure into the
domain. The request carries no instructions text — only a role and a
typed payload snapshot; prompt wording belongs to the adapter.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.schemas import MAX_ITEM_CHARS
from nomadicos.contracts.core import Contract

MAX_PAYLOAD_KEYS = 32
MAX_PAYLOAD_JSON_CHARS = 32768


class CognitiveRole(StrEnum):
    """Cognitive role a model is invoked for (spec §32 role routing)."""

    DECOMPOSER = "DECOMPOSER"
    ACTOR = "ACTOR"
    MONITOR = "MONITOR"


class CognitiveRequest(Contract):
    """Typed input for one structured model call.

    Deterministic and serializable: the same brain state snapshot always
    yields the same request. No prompt text, no provider specifics.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: CognitiveRole
    mission_id: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> CognitiveRequest:
        if not self.mission_id.strip():
            raise ValueError("cognitive request mission_id must not be empty")
        if len(self.payload) > MAX_PAYLOAD_KEYS:
            raise ValueError(f"cognitive request payload exceeds {MAX_PAYLOAD_KEYS} keys")
        for key in self.payload:
            if not isinstance(key, str) or len(key) > MAX_ITEM_CHARS:
                raise ValueError("cognitive request payload keys must be bounded strings")
        return self

    def to_canonical_json(self) -> str:
        """Deterministic canonical serialization."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@runtime_checkable
class StructuredGenerator(Protocol):
    """The ONLY model seam the brain may know (spec §8, Sprint 2).

    Implementations live outside the brain (inference adapter layer):
    they own prompt construction, engine selection, timeouts, and
    provider details. The brain consumes raw JSON text and validates it
    into typed contracts itself — malformed output fails closed.
    """

    async def generate_structured(self, request: CognitiveRequest) -> str: ...


def parse_model_json(text: str, *, context: str) -> dict[str, Any]:
    """Strictly parse model output into a JSON object.

    Fail closed: non-object JSON, empty output, code fences, prose, and
    oversized documents are all rejected with ``ModelOutputRejected`` —
    never leniently parsed, never partially accepted.
    """
    if len(text) > MAX_PAYLOAD_JSON_CHARS * 4:
        raise ModelOutputRejected(
            f"{context}: model output exceeds the size bound",
            output_chars=len(text),
        )
    stripped = text.strip()
    if not stripped:
        raise ModelOutputRejected(f"{context}: model output is empty")
    if stripped.startswith("```"):
        raise ModelOutputRejected(
            f"{context}: model output must be a bare JSON object, not fenced text"
        )
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelOutputRejected(
            f"{context}: model output is not valid JSON ({exc.msg} at line {exc.lineno})"
        ) from None
    if not isinstance(parsed, dict):
        raise ModelOutputRejected(
            f"{context}: model output must be a JSON object, got {type(parsed).__name__}"
        )
    if len(parsed) > MAX_PAYLOAD_KEYS:
        raise ModelOutputRejected(
            f"{context}: model output exceeds {MAX_PAYLOAD_KEYS} top-level keys"
        )
    return parsed
