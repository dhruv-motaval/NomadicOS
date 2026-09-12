"""Canonical task/action IR (rebuild plan §2–§3, §6, §7).

Trust boundary enforced here:

    UNTRUSTED MODEL TEXT
      -> ActionClaim.from_model_text()   (strict validation; model claims only)
      -> TaskAction.bind()               (identity + risk + capabilities bound
                                          by the SYSTEM registry, not the model)
      -> policy / executor               (consume TaskAction, never raw payload)

The model can only ever claim one of three operations: TOOL_CALL, REPLY, or
FINISH. Anything malformed, oversized, or authority-claiming becomes an
``ActionKind.INVALID`` claim — which the loop must treat as a failed step,
never as finished/success/approved (BP §6, §86, §366). Identity fields and
risk/capability claims in the payload are rejected or ignored: authority to
execute is never a model-supplied field.
"""
from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import ValidationError
from nomadicos.security.permissions import SubjectIdentity

IR_SCHEMA_VERSION: Final = "1"

# Resource bounds — reject, never guess (BP §142).
MAX_CLAIM_JSON_CHARS = 8_000
MAX_ARGUMENT_KEYS = 64
MAX_ARGUMENTS_JSON_BYTES = 6_000

# Keys the model MUST NOT send: claims of authority/security-relevance.
# A payload containing any of them is rejected outright, with a specific
# reason (rebuild plan §3: the model cannot manufacture authorization).
AUTHORITY_FIELDS = frozenset(
    {
        "allowed",
        "allow",
        "approve",
        "approved",
        "authorize",
        "authorized",
        "authorization",
        "bypass_policy",
        "bypass",
        "capabilities",
        "capability",
        "grant",
        "grants",
        "permission",
        "permissions",
        "privileged",
        "privilege",
        "risk",
    }
)

# Keys that are pure identity noise from the model: IGNORED (bound by the
# system in TaskAction.bind, never taken from the payload).
IGNORED_IDENTITY_FIELDS = frozenset({"task_id", "step_id", "run_id"})

_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


class ActionKind(StrEnum):
    TOOL_CALL = "tool_call"
    REPLY = "reply"
    FINISH = "finish"
    INVALID = "invalid"


def extract_json_object(text: str) -> dict[str, Any] | None:
    """First balanced JSON object in model text (reasoning may contain braces)."""
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for end in range(start, len(text)):
            c = text[end]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : end + 1])
                    except json.JSONDecodeError:
                        break  # not a real object at this brace; scan the next
                    if isinstance(parsed, dict):
                        return parsed
                    return None
    return None


def _check_arguments(arguments: dict[str, Any]) -> None:
    """Argument *shape* bounds (type/required/enum validation per-tool remains
    at the gateway, BP §142). Malformed model output must fail here, before
    anything reaches policy or the executor."""
    if not isinstance(arguments, dict):
        raise ValidationError("arguments must be an object")
    if len(arguments) > MAX_ARGUMENT_KEYS:
        raise ValidationError(f"too many argument keys ({len(arguments)})")
    for key in arguments:
        if not isinstance(key, str) or len(key) > 128:
            raise ValidationError("argument keys must be short strings")
    try:
        dumped = json.dumps(arguments)
    except (TypeError, ValueError) as exc:
        raise ValidationError("arguments must be JSON-serializable") from exc
    if len(dumped.encode("utf-8", errors="replace")) > MAX_ARGUMENTS_JSON_BYTES:
        raise ValidationError("arguments too large")


class _ModelPayload(BaseModel):
    """Strict view of what the model may send. Extra keys are rejected; the
    dangerous-authority denylist is checked before this model because pydantic
    would hide WHICH field was offensive behind a generic message."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    schema_version: Literal["1"] = IR_SCHEMA_VERSION
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reply: str | None = None
    reasoning: str | None = Field(
        default=None, validation_alias=AliasChoices("reasoning", "thought", "analysis")
    )
    finished: bool = False

    @field_validator("tool")
    @classmethod
    def _tool_name_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _TOOL_NAME_RE.fullmatch(v):
            raise ValueError("tool name must be lowercase [a-z][a-z0-9_.-]{0,63}")
        return v

    @field_validator("reply")
    @classmethod
    def _reply_size(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 4_000:
            raise ValueError("reply too large")
        return v

    @field_validator("arguments")
    @classmethod
    def _arguments_shape(cls, v: dict[str, Any]) -> dict[str, Any]:
        _check_arguments(v)
        return v


class ActionClaim(BaseModel):
    """Validated model claim — trusted only as *data about what the model
    said*, never as authority to act. Production code consumes this only via
    ``TaskAction`` after the system binds identity/risk/capabilities."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = IR_SCHEMA_VERSION
    kind: ActionKind
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reply: str | None = None
    finish: bool = False
    reasoning: str | None = None
    reason: str | None = None  # set when kind == INVALID

    @classmethod
    def invalid(cls, reason: str) -> ActionClaim:
        return cls(kind=ActionKind.INVALID, reason=reason[:300])

    @classmethod
    def from_model_text(cls, text: str) -> ActionClaim:
        """THE parser (rebuild plan 'MODEL OUTPUT → parser → IR validation')."""
        payload_text = text.strip()
        if len(payload_text) > MAX_CLAIM_JSON_CHARS:
            return cls.invalid("model output too large")
        obj = extract_json_object(payload_text)
        if obj is None:
            return cls.invalid("model output contained no JSON object")

        lowered = {str(k).strip().lower() for k in obj}
        bad = sorted(lowered & AUTHORITY_FIELDS)
        if bad:
            return cls.invalid(f"model output may not claim authority: {bad}")

        cleaned = {
            k: v for k, v in obj.items() if str(k).strip().lower() not in IGNORED_IDENTITY_FIELDS
        }
        try:
            parsed = _ModelPayload.model_validate(cleaned)
        except Exception as exc:  # pydantic ValidationError et al — one message
            first = str(exc).splitlines()[0] if str(exc).splitlines() else str(exc)
            return cls.invalid(f"model output failed IR validation: {first[:180]}")

        if parsed.tool:
            return cls(
                kind=ActionKind.TOOL_CALL,
                tool=parsed.tool,
                arguments=parsed.arguments,
                finish=parsed.finished,
                reasoning=parsed.reasoning,
            )
        if parsed.reply:
            return cls(
                kind=ActionKind.REPLY,
                reply=parsed.reply,
                finish=parsed.finished,
                reasoning=parsed.reasoning,
            )
        if "finished" in cleaned and parsed.finished:
            return cls(
                kind=ActionKind.FINISH, finish=True, reasoning=parsed.reasoning
            )
        return cls.invalid("model output has no tool, reply, or explicit finish field")


class TaskAction(BaseModel):
    """The canonical, execution-ready IR.

    Constructed only by ``bind`` — which injects run identity (task/step/attempt)
    and *system-derived* risk + capabilities. Nothing from model text flows in
    except tool/arguments/reply/finish (already validated in ActionClaim)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = IR_SCHEMA_VERSION
    task_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=4)
    kind: ActionKind
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reply: str | None = None
    finish: bool = False
    risk: RiskLevel
    capabilities: tuple[str, ...] = ()
    origin_model: str | None = None

    @field_validator("kind")
    @classmethod
    def _not_invalid(cls, v: ActionKind) -> ActionKind:
        if v is ActionKind.INVALID:
            raise ValueError("invalid claims cannot bind into runnable actions")
        return v

    @classmethod
    def bind(
        cls,
        claim: ActionClaim,
        *,
        task_id: str,
        step_id: str,
        attempt: int,
        risk: RiskLevel,
        capabilities: tuple[str, ...],
        origin_model: str | None = None,
    ) -> TaskAction:
        """Canonical boundary function: validated claim + SYSTEM-supplied
        metadata -> the only object policy and the executor will accept."""
        if claim.kind is ActionKind.INVALID:
            raise ValidationError(
                f"cannot bind invalid claim: {claim.reason or 'unspecified'}"
            )
        return cls(
            kind=claim.kind,
            tool=claim.tool,
            arguments=dict(claim.arguments),
            reply=claim.reply,
            finish=claim.finish,
            task_id=task_id,
            step_id=step_id,
            attempt=attempt,
            risk=risk,
            capabilities=capabilities,
            origin_model=origin_model,
        )

    def identity_for(self, base: SubjectIdentity) -> SubjectIdentity:
        """Step-scoped identity (BP §364/§399 correlation). Stable per
        (task, attempt, step); retries share task_id, differ on step_id."""
        from dataclasses import replace

        return replace(base, task_id=self.task_id, step_id=self.step_id)

    def label(self) -> str:
        return f"{self.tool or self.kind} {json.dumps(self.arguments)[:120]}"

    def to_json(self) -> str:
        """Deterministic serialization (field order fixed by class body)."""
        return self.model_dump_json()


__all__ = [
    "ActionClaim",
    "ActionKind",
    "TaskAction",
    "extract_json_object",
    "IR_SCHEMA_VERSION",
    "AUTHORITY_FIELDS",
]
