"""Deterministic model-output parser (SPEC §19).

Accepted shapes (exactly one, unambiguous):
  1. the whole output is one JSON object; or
  2. the output contains exactly one fenced ```json / ``` block whose body is
     one JSON object (surrounding prose is allowed only around a fence).

Everything else — empty, null, arrays, multiple candidate objects, invalid
JSON, duplicate keys, authority-bearing keys at any depth, action keys mixed
with ``finished`` — is rejected as INVALID_PROPOSAL. Unsafe output is never
"fixed" into an executable action.
"""

from __future__ import annotations

import json
import re
from typing import Any

from nomadicos.contracts.action import (
    MODEL_AUTHORITY_FIELDS,
    ActionProposal,
    CompletionClaim,
)
from nomadicos.kernel.errors import InvalidProposal

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.IGNORECASE | re.DOTALL)

_PROPOSAL_KEYS = frozenset({"tool", "operation", "args", "note"})
_CLAIM_KEYS = frozenset({"finished", "justification"})


def extract_json_candidate(raw: str) -> str:
    """Deterministically select THE single JSON candidate, or reject."""
    stripped = raw.strip()
    if not stripped:
        raise InvalidProposal("empty model output")
    if stripped[0] in "{[":
        return stripped
    fences = _FENCE_RE.findall(stripped)
    if len(fences) == 1:
        body = fences[0].strip()
        if not body:
            raise InvalidProposal("fenced code block is empty")
        return body
    if len(fences) > 1:
        raise InvalidProposal(f"ambiguous output: {len(fences)} fenced JSON blocks")
    raise InvalidProposal("no JSON object found in model output")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InvalidProposal(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _walk_authority(node: Any, path: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() in MODEL_AUTHORITY_FIELDS:
                raise InvalidProposal(
                    f"model-authored authority field {key!r} at "
                    f"{path or '<root>'} is rejected (SPEC \u00a719)"
                )
            _walk_authority(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_authority(item, f"{path}[{i}]")


def reject_authority_fields(obj: Any) -> None:
    _walk_authority(obj, "")


class ParsedProposal:
    """Either an ActionProposal or a CompletionClaim — never both."""

    def __init__(self, value: ActionProposal | CompletionClaim) -> None:
        self.value = value

    @property
    def is_claim(self) -> bool:
        return isinstance(self.value, CompletionClaim)


def parse_model_output(
    raw: str | Any,
    *,
    task_id: str,
    step_id: str,
    model_id: str,
    attempt: int = 1,
) -> ParsedProposal:
    """Parse untrusted model output into the canonical typed proposal.

    Correlation identity (task/step/model/attempt) is system-supplied and
    overrides anything the model tried to claim, so forged IDs are impossible.
    """
    if raw is None:
        raise InvalidProposal("null model output")
    if not isinstance(raw, str):
        raise InvalidProposal(f"model output must be text, got {type(raw).__name__}")
    candidate = extract_json_candidate(raw)
    try:
        obj = json.loads(candidate, object_pairs_hook=_reject_duplicates)
    except InvalidProposal:
        raise
    except json.JSONDecodeError as exc:
        raise InvalidProposal(f"invalid JSON: {exc.msg} (line {exc.lineno})") from exc
    if obj is None:
        raise InvalidProposal("JSON null")
    if not isinstance(obj, dict):
        raise InvalidProposal(f"top-level JSON must be an object, got {type(obj).__name__}")

    reject_authority_fields(obj)

    keys = set(obj)
    if "finished" in keys:
        if not keys <= _CLAIM_KEYS:
            raise InvalidProposal("completion claim cannot be mixed with action fields")
        finished = obj["finished"]
        if not isinstance(finished, bool):
            raise InvalidProposal("'finished' must be boolean")
        return ParsedProposal(
            CompletionClaim(
                task_id=task_id,
                model_id=model_id,
                claim=finished,
                justification=str(obj.get("justification", "")),
            )
        )

    unknown = keys - _PROPOSAL_KEYS
    if unknown:
        raise InvalidProposal(f"unexpected top-level field(s): {sorted(unknown)}")
    tool, operation = obj.get("tool"), obj.get("operation")
    if not isinstance(tool, str) or not tool:
        raise InvalidProposal("missing/invalid 'tool'")
    if not isinstance(operation, str) or not operation:
        raise InvalidProposal("missing/invalid 'operation'")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise InvalidProposal("'args' must be an object")
    note = obj.get("note", "")
    if not isinstance(note, str):
        raise InvalidProposal("'note' must be a string")

    # Deterministic canonicalization ONLY (SPEC §19): a model that spells the
    # action as tool="filesystem.write", operation="write" carries no
    # ambiguity — the trailing segment must equal its own operation field.
    # Anything else ("a.b" + "c") is left untouched and fails validation.
    if "." in tool and tool.split(".", 1)[1] == operation:
        tool = tool.split(".", 1)[0]

    try:
        proposal = ActionProposal(
            task_id=task_id,
            step_id=step_id,
            attempt=attempt,
            model_id=model_id,
            tool=tool,
            operation=operation,
            args=args,
            note=note,
        )
    except Exception as exc:  # pydantic schema violations fail closed
        raise InvalidProposal(f"proposal schema violation: {exc}") from exc
    return ParsedProposal(proposal)
