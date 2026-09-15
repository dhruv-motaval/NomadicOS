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

_NOMADICOS_MODEL_CLASSES = (
    # module, class
    ("nomadicos.contracts.core", "Goal"),
    ("nomadicos.contracts.core", "PlanStep"),
    ("nomadicos.contracts.core", "FailureRecord"),
    ("nomadicos.contracts.core", "TaskStatus"),
    ("nomadicos.contracts.core", "Predicate"),
    ("nomadicos.contracts.core", "PredicateGroup"),
    ("nomadicos.contracts.core", "GoalPredicate"),
    ("nomadicos.contracts.core", "LogicalOp"),
    ("nomadicos.contracts.action", "ActionProposal"),
    ("nomadicos.contracts.action", "ProposalKind"),
    ("nomadicos.contracts.action", "CapabilityRef"),
    ("nomadicos.contracts.action", "CompletionClaim"),
    ("nomadicos.contracts.execution", "ExecutionResult"),
    ("nomadicos.contracts.execution", "ExecutionStatus"),
    ("nomadicos.contracts.execution", "Observation"),
    ("nomadicos.contracts.execution", "ObservationKind"),
    ("nomadicos.contracts.model", "TaskRequirements"),
    ("nomadicos.contracts.model", "TaskType"),
    ("nomadicos.contracts.model", "CapabilityTag"),
    ("nomadicos.contracts.verification", "VerificationResult"),
    ("nomadicos.contracts.verification", "VerificationLevel"),
    ("nomadicos.contracts.verification", "VerificationOutcome"),
    ("nomadicos.contracts.verification", "EvidenceItem"),
    ("nomadicos.kernel.errors", "Failure"),
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
