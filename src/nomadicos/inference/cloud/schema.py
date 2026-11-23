"""Fail-closed structured-output validation (SPEC §10).

A compact JSON-Schema validator for the closed schemas the Brain derives from
its own pydantic payload contracts. Unknown VALIDATION keywords are an error
(never silently accepted), so a schema this module cannot enforce fails
closed instead of passing everything. The Brain still re-validates; this is
the inference-boundary gate so invalid output never enters Brain state.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: annotation-only keywords that carry no constraint
_ANNOTATIONS = {"title", "description", "default", "examples", "$schema", "$id", "$defs", "format"}
_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class SchemaError(ValueError):
    pass


def extract_json(text: str) -> Any:
    """Parse model text as one JSON value (tolerating a code fence or leading
    prose). Raises ``SchemaError`` if none is found."""
    body = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```\s*$", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    try:
        return json.loads(body)
    except ValueError:
        pass
    start = min((i for i in (body.find("{"), body.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise SchemaError("no JSON value in model output")
    try:
        value, _ = json.JSONDecoder().raw_decode(body[start:])
    except ValueError as exc:
        raise SchemaError(f"invalid JSON: {exc}") from exc
    return value


def _is(value: Any, name: str) -> bool:
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name in ("string", "object", "array", "null"):
        return isinstance(value, _TYPES[name])
    if name == "boolean":
        return isinstance(value, bool)
    raise SchemaError(f"unsupported type {name!r}")


def validate(value: Any, schema: dict[str, Any], root: dict[str, Any] | None = None) -> None:
    """Raise ``SchemaError`` unless ``value`` conforms to ``schema``."""
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise SchemaError(f"unsupported $ref {ref!r}")
        node: Any = root
        for part in ref[2:].split("/"):
            node = node[part] if isinstance(node, dict) and part in node else None
        if not isinstance(node, dict):
            raise SchemaError(f"unresolvable $ref {ref!r}")
        validate(value, node, root)
        return
    for key, arg in schema.items():
        if key in _ANNOTATIONS:
            continue
        if key == "type":
            names = arg if isinstance(arg, list) else [arg]
            if not any(_is(value, n) for n in names):
                raise SchemaError(f"expected {arg}, got {type(value).__name__}")
        elif key == "enum":
            if value not in arg:
                raise SchemaError(f"{value!r} not in enum")
        elif key == "const":
            if value != arg:
                raise SchemaError(f"{value!r} != const {arg!r}")
        elif key == "properties":
            if isinstance(value, dict):
                for name, sub in arg.items():
                    if name in value:
                        validate(value[name], sub, root)
        elif key == "required":
            if isinstance(value, dict):
                missing = [n for n in arg if n not in value]
                if missing:
                    raise SchemaError(f"missing required {missing}")
        elif key == "additionalProperties":
            if isinstance(value, dict):
                extra = [n for n in value if n not in schema.get("properties", {})]
                if arg is False and extra:
                    raise SchemaError(f"unexpected properties {extra}")
                if isinstance(arg, dict):
                    for n in extra:
                        validate(value[n], arg, root)
        elif key == "items":
            if isinstance(value, list):
                for item in value:
                    validate(item, arg, root)
        elif key in ("minItems", "maxItems"):
            if isinstance(value, list) and (
                len(value) < arg if key == "minItems" else len(value) > arg
            ):
                raise SchemaError(f"{key} {arg} violated")
        elif key in ("minLength", "maxLength"):
            if isinstance(value, str) and (
                len(value) < arg if key == "minLength" else len(value) > arg
            ):
                raise SchemaError(f"{key} {arg} violated")
        elif key in ("minimum", "maximum"):
            if _is(value, "number") and (value < arg if key == "minimum" else value > arg):
                raise SchemaError(f"{key} {arg} violated")
        elif key == "pattern":
            if isinstance(value, str) and not re.search(arg, value):
                raise SchemaError("pattern mismatch")
        elif key in ("anyOf", "oneOf"):
            passed = 0
            for sub in arg:
                try:
                    validate(value, sub, root)
                    passed += 1
                except SchemaError:
                    pass
            if passed == 0 or (key == "oneOf" and passed != 1):
                raise SchemaError(f"{key} not satisfied")
        elif key == "allOf":
            for sub in arg:
                validate(value, sub, root)
        else:
            raise SchemaError(f"unsupported schema keyword {key!r}")


def validate_text(text: str, schema: dict[str, Any]) -> None:
    validate(extract_json(text), schema)
