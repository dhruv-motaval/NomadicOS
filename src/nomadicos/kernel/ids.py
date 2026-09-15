"""Correlatable identifiers (SPEC §35 correlation, §46 task isolation)."""

from __future__ import annotations

import uuid

_PREFIXES = (
    "task",
    "step",
    "prop",
    "grant",
    "authz",
    "exec",
    "obs",
    "verif",
    "critic",
    "mem",
    "obj",
    "rel",
    "model",
    "bench",
    "evt",
    "run",
    "session",
)


def new_id(prefix: str) -> str:
    """Return a sortable, collision-resistant identifier with a known prefix."""
    if prefix not in _PREFIXES:
        raise ValueError(f"unknown id prefix {prefix!r}; expected one of {_PREFIXES}")
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
