"""Bounded deterministic soak harness (Sprint 10).

Runs a representative Brain workflow repeatedly against fresh frozen
states and measures the hardening invariants that matter in production:

- state corruption: the workflow must return a NEW frozen state with a
  strictly increased version (never mutate, never stall);
- duplicate events: event ids must be unique within a state and must
  never repeat across iterations (stale/corrupt trajectory signal);
- bounded growth: attention items stay within the Sprint 6 constant;
- exception accumulation: every failure is recorded honestly (bounded
  list) and never retried unboundedly;
- nondeterministic outputs: the whole soak is replayed once and the
  per-iteration outcome fingerprints must match exactly (timestamps and
  random ids honestly count as nondeterminism — injectable clocks/ids
  make workflows replayable).

Bounded by construction: ``iterations`` ≤ MAX_SOAK_ITERATIONS (CI-friendly
default 10, larger optional stress values remain bounded). No sleeps, no
infinite loops, no live models, no network.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.schemas import MAX_ATTENTION_ITEMS
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract

MAX_SOAK_ITERATIONS = 128
MAX_SOAK_FAILURES = 8


class SoakWorkflow(Protocol):
    """One representative cognitive pass: state in, NEW state out."""

    def __call__(self, state: BrainState, iteration: int) -> BrainState: ...


class StateFactory(Protocol):
    def __call__(self) -> BrainState: ...


class SoakIteration(Contract):
    """One soak iteration's measured outcome (DATA only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    iteration: int = Field(ge=0)
    ok: bool
    state_version: int = Field(default=0, ge=0)
    event_count: int = Field(default=0, ge=0)
    attention_items: int = Field(default=0, ge=0)
    info_requests: int = Field(default=0, ge=0)
    detail: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _coherent(self) -> SoakIteration:
        if not self.ok and not self.detail.strip():
            raise ValueError("a failed soak iteration requires a detail")
        return self


class SoakReport(Contract):
    """Deterministic soak summary (bounded; auditable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    iterations: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    #: state version after the final pass (strictly increasing invariant)
    final_version: int = Field(ge=0)
    #: event ids that repeated ACROSS iterations (must be 0)
    duplicate_events: int = Field(ge=0)
    #: True when a full replay reproduces every outcome fingerprint
    deterministic: bool = True
    failures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> SoakReport:
        if len(self.failures) > MAX_SOAK_FAILURES:
            raise ValueError(f"soak failures exceed {MAX_SOAK_FAILURES}")
        if self.passed + self.failed != self.iterations:
            raise ValueError("soak counts must sum to iterations")
        return self


def _fingerprint(state: BrainState) -> tuple:
    """Deterministic outcome projection (freeze-relevant shape only)."""
    return (
        state.state_version,
        len(state.history),
        tuple(ref.event_id for ref in state.history[-8:]),
        tuple(item.ref for item in state.attention.items) if state.attention else (),
        tuple(request.target for request in state.information_requests),
    )


def run_soak(
    workflow: SoakWorkflow,
    state_factory: StateFactory,
    *,
    iterations: int = 10,
) -> SoakReport:
    """Run the bounded deterministic soak. Failures are recorded, never
    swallowed and never retried unboundedly; determinism is verified by a
    full replay whose outcome fingerprints must match."""
    iterations = min(max(iterations, 0), MAX_SOAK_ITERATIONS)
    failures: list[str] = []
    outcomes: list[SoakIteration] = []
    fingerprints: list[tuple] = []
    seen_event_ids: set[str] = set()
    duplicate_events = 0

    for iteration in range(iterations):
        try:
            state = state_factory()
            before = state.model_dump(mode="json")
            result = workflow(state, iteration)
            if result is state:
                raise ValueError("workflow must return a NEW state (frozen transitions)")
            if result.state_version <= state.state_version:
                raise ValueError(
                    f"state version must strictly increase "
                    f"({state.state_version} -> {result.state_version})"
                )
            if result.model_dump(mode="json") == before:
                raise ValueError("workflow returned an unchanged state")
            attention_items = len(result.attention.items) if result.attention else 0
            if attention_items > MAX_ATTENTION_ITEMS:
                raise ValueError(
                    f"attention grew beyond the {MAX_ATTENTION_ITEMS}-item bound"
                )
            within_state = len(result.history) - len(
                {ref.event_id for ref in result.history}
            )
            if within_state:
                raise ValueError(f"{within_state} duplicate event ids in one state")
            ids = {ref.event_id for ref in result.history}
            duplicate_events += len(ids & seen_event_ids)
            seen_event_ids |= ids
            fingerprints.append(_fingerprint(result))
            outcomes.append(
                SoakIteration(
                    iteration=iteration,
                    ok=True,
                    state_version=result.state_version,
                    event_count=len(result.history),
                    attention_items=attention_items,
                    info_requests=len(result.information_requests),
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded honestly, bounded
            outcomes.append(
                SoakIteration(
                    iteration=iteration,
                    ok=False,
                    detail=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
            )

    failed = iterations - passed_count(outcomes)
    if iterations and not failed:
        # determinism: full replay, same factory/workflow, compare shapes
        replay: list[tuple] = []
        try:
            for iteration in range(iterations):
                state = state_factory()
                result = workflow(state, iteration)
                replay.append(_fingerprint(result))
            deterministic = replay == fingerprints
        except Exception as exc:  # noqa: BLE001 - replay failure is honest
            deterministic = False
            failures.append(f"replay failed: {type(exc).__name__}")
    else:
        deterministic = not failed

    recorded = [f"iteration {o.iteration}: {o.detail}" for o in outcomes if not o.ok]
    if duplicate_events:
        recorded.append(f"{duplicate_events} event ids repeated across iterations")
    return SoakReport(
        iterations=iterations,
        passed=passed_count(outcomes),
        failed=failed,
        final_version=outcomes[-1].state_version if outcomes else 0,
        duplicate_events=duplicate_events,
        deterministic=deterministic,
        failures=(recorded + failures)[:MAX_SOAK_FAILURES],
    )


def passed_count(outcomes: list[SoakIteration]) -> int:
    return sum(1 for outcome in outcomes if outcome.ok)
