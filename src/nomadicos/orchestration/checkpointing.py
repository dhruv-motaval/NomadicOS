"""Checkpoint serialization for NomadicOS state objects.

State values include typed contracts (Goal, ActionProposal, ...). We give
LangGraph's serializer an EXPLICIT allow-list of our own classes rather than
enabling pickle or unrestricted types: checkpoints stay introspectable,
supply-chain-safe, and Phase 13 can swap in the PostgreSQL saver without
changing graph code (SPEC §47, §7.22: orchestration checkpoints are NOT the
durable source of truth).
"""

from __future__ import annotations

from typing import Any

from nomadicos.persistence.checkpoints import (
    NOMADICOS_SERDE_CLASSES as _NOMADICOS_MODEL_CLASSES,
)


def make_saver() -> Any:
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    except ImportError:  # pragma: no cover - langgraph always present here
        raise
    try:
        serde = JsonPlusSerializer(allowed_msgpack_modules=list(_NOMADICOS_MODEL_CLASSES))
        return InMemorySaver(serde=serde)
    except TypeError:  # older/newer langgraph signature: fall back, warnings only
        return InMemorySaver()


def make_durable_saver(persistence) -> Any:
    """Durable checkpoint saver selected by PersistenceConfig (Phase 13B,
    SPEC §34/§47): PostgreSQL when dsn is configured, otherwise the
    deterministic file adapter under state_dir. Both reuse the InMemorySaver
    serialization semantics via the existing allow-list; orchestration
    checkpoints remain orchestration state - NOT the authority path."""
    from nomadicos.persistence.checkpoints import make_durable_saver as _make_durable

    return _make_durable(persistence)
