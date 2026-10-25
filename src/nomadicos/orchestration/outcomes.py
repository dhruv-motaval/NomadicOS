"""Execution outcome classification for safe recovery (Phase 2, SPEC §30).

Before any retry, the recovery gateway classifies the previous execution
outcome from DURABLE/OBSERVABLE data only — the last execution result
for this step, the executor-refusal flag, the failure category, and the
durable execution ledger. Model prose is never consulted.

Classes (SPEC §30 taxonomy; never collapsed):

- DENIAL                      validation/authorization refusal (policy fact;
                              a re-proposal may differ, but the denial itself
                              is never "transient")
- NO_EXECUTION                executor refused before running anything
- SIDE_EFFECT_CONFIRMED       execution result SUCCEEDED and is durably recorded
- EXECUTION_FAILED_RECORDED   tool ran and reported failure; the attempt
                              identity is durably recorded (replay blocked)
- OUTCOME_UNKNOWN             a side effect may have occurred but no durable
                              record proves what happened (crash window)
- TRANSIENT                   model/routing/infrastructure failure, nothing ran

The classification is DATA for the recovery decision. It never authorizes,
executes, or decides success; the authoritative verifier still owns SUCCESS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any

from nomadicos.contracts.execution import ExecutionResult

#: categories that are POLICY facts or hard stops, never transient model
#: problems: escalating the model cannot change any of them
DENIAL_CATEGORIES = frozenset(
    {
        "INVALID_PROPOSAL",
        "AUTHORIZATION_DENIED",
        "REVOKED",
        "CONFIG_INVALID",
        "BUDGET_EXHAUSTED",
    }
)

_LEDGER_UNRECORDED = "unrecorded"


def current_step_id(state: Mapping[str, Any]) -> str | None:
    """The runtime-owned step identity for the current plan position."""
    plan = state.get("plan") or []
    idx = state.get("current_step", 0)
    if plan and 0 <= idx < len(plan):
        return plan[idx].id
    return None


class OutcomeClass(StrEnum):
    """What the previous execution outcome is KNOWN to be (Phase 2)."""

    DENIAL = "DENIAL"
    NO_EXECUTION = "NO_EXECUTION"
    SIDE_EFFECT_CONFIRMED = "SIDE_EFFECT_CONFIRMED"
    EXECUTION_FAILED_RECORDED = "EXECUTION_FAILED_RECORDED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    TRANSIENT = "TRANSIENT"


def classify_last_outcome(
    state: Mapping[str, Any],
    *,
    recorded: Callable[[str], object] | None = None,
) -> OutcomeClass:
    """Classify the PREVIOUS execution outcome before any retry decision.

    Pure and deterministic: reads only the typed execution results for the
    CURRENT step, the executor-refusal flag, and (via ``recorded``) the
    durable ledger. An unrecorded side effect that the ledger cannot
    confirm is UNKNOWN — the caller must stop instead of replaying
    (SPEC §56.14: no automatic retry across the durability gap).
    """
    executions = list(state.get("executions") or [])
    step_id = current_step_id(state)
    if step_id is not None:
        executions = [e for e in executions if e.step_id == step_id]
    last_exec: ExecutionResult | None = executions[-1] if executions else None
    if last_exec is not None:
        if str(last_exec.evidence.get("durable_ledger", "")) == "unrecorded":
            # crash window: the side effect may exist but no durable record
            # proves the outcome - reconcile against the durable ledger
            if recorded is None or recorded(last_exec.action_fingerprint) is None:
                return OutcomeClass.OUTCOME_UNKNOWN
        if last_exec.succeeded:
            return OutcomeClass.SIDE_EFFECT_CONFIRMED
        # the tool ran and honestly reported failure; the attempt identity
        # IS durably recorded, so a re-proposal cannot duplicate the effect
        return OutcomeClass.EXECUTION_FAILED_RECORDED
    if state.get("execution_missing"):
        # the executor refused before producing a result: no side effect
        return OutcomeClass.NO_EXECUTION
    category = str((state.get("last_failure") or {}).get("category", ""))
    if category in DENIAL_CATEGORIES:
        return OutcomeClass.DENIAL
    return OutcomeClass.TRANSIENT


__all__ = [
    "DENIAL_CATEGORIES",
    "OutcomeClass",
    "classify_last_outcome",
    "current_step_id",
]
