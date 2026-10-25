"""Complete execution trace reconstruction (Sprint 10 observability).

Every representative Brain execution must be reconstructable from trace
data. ``reconstruct_trace`` is a PURE function over injected artifacts
(the BrainEvent list the composition layer already collects, plus the
real ExecutionResult/VerificationResult references when available):

    goal → attention → uncertainty → metacognition → effort → search →
    action selection → proposal → execution → verification → outcome

HARD invariants (enforced here, tested in tests):
- PROPOSED ≠ EXECUTED: an execution stage exists ONLY for real
  ``ExecutionResult`` references — never for a proposal event;
- EXECUTION SUCCESS ≠ VERIFICATION SUCCESS: both stages are recorded
  separately with their own outcomes;
- failures are explicit stages, never silent;
- secrets/_payloads are not embedded in traces — stage names, ids, and
  outcomes only (bounded).
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract
from nomadicos.contracts.execution import ExecutionResult
from nomadicos.contracts.verification import VerificationOutcome, VerificationResult

MAX_TRACE_STEPS = 64

#: the cognitive stage chain (deterministic order), BrainEventType → stage
_STAGE_BY_EVENT: dict[BrainEventType, str] = {
    BrainEventType.BRAIN_ATTENTION_FOCUSED: "attention",
    BrainEventType.BRAIN_BELIEFS_ASSESSED: "uncertainty",
    BrainEventType.BRAIN_METACOGNITION_PRODUCED: "metacognition",
    BrainEventType.BRAIN_INFO_GATHERING_REQUESTED: "information_gathering",
    BrainEventType.BRAIN_EFFORT_DECIDED: "effort",
    BrainEventType.BRAIN_PREDICTION_PRODUCED: "prediction",
    BrainEventType.BRAIN_EVALUATION_PRODUCED: "evaluation",
    BrainEventType.BRAIN_SEARCH_STARTED: "search",
    BrainEventType.BRAIN_SEARCH_EXPANDED: "search",
    BrainEventType.BRAIN_SEARCH_COMPLETED: "search",
    BrainEventType.BRAIN_ACTION_SELECTED: "proposal",
}


class TraceStep(Contract):
    """One bounded trace stage (names/ids/outcomes — never payloads)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(max_length=64)
    event_id: str = Field(default="", max_length=128)
    ok: bool = True
    detail: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _coherent(self) -> TraceStep:
        if not self.stage.strip():
            raise ValueError("trace stage must not be empty")
        return self


class ExecutionTrace(Contract):
    """Reconstructed execution trace (bounded; auditable)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    steps: list[TraceStep] = Field(default_factory=list)
    #: cognitive stages covered (for completeness checking)
    stages: list[str] = Field(default_factory=list)
    #: PROPOSED (cognitive selection) count — never counted as executed
    proposed_count: int = Field(ge=0)
    #: real executions (each carries its own id + status)
    executed_ids: list[str] = Field(default_factory=list)
    failed_executions: list[str] = Field(default_factory=list)
    verification_outcome: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> ExecutionTrace:
        if len(self.steps) > MAX_TRACE_STEPS:
            raise ValueError(f"trace steps exceed {MAX_TRACE_STEPS}")
        if len(self.stages) > MAX_TRACE_STEPS or len(self.executed_ids) > MAX_TRACE_STEPS:
            raise ValueError("trace out of bounds")
        return self


def reconstruct_trace(
    state: BrainState,
    events: list[BrainEvent],
    *,
    executions: list[ExecutionResult] | None = None,
    verification: VerificationResult | None = None,
) -> ExecutionTrace:
    """Pure deterministic trace reconstruction.

    ``events`` is the composition layer's collected BrainEvent list; the
    goal stage comes from the state itself. Execution and verification
    stages appear ONLY from the real artifacts passed in — a trace can
    never claim an execution that did not happen."""
    steps: list[TraceStep] = [TraceStep(stage="goal", detail=state.goal.objective[:180])]
    stages: list[str] = ["goal"]
    proposed_count = 0
    for event in events:
        stage = _STAGE_BY_EVENT.get(event.event_type)
        if stage is None:
            continue  # non-trace events (e.g. model probes) are skipped
        ok = event.event_type not in (BrainEventType.BRAIN_SEARCH_COMPLETED,) or bool(event.payload)
        steps.append(TraceStep(stage=stage, event_id=event.event_id, ok=ok))
        if stage == "proposal":
            proposed_count += 1
        if stage not in stages:
            stages.append(stage)

    executed_ids: list[str] = []
    failed_executions: list[str] = []
    for execution in executions or []:
        executed_ids.append(execution.id)
        ok = execution.status is not None and execution.status.value in ("SUCCEEDED",)
        if not ok:
            failed_executions.append(execution.id)
        steps.append(
            TraceStep(
                stage="execution",
                event_id=execution.id,
                ok=ok,
                detail=f"status={execution.status.value}",
            )
        )
        if "execution" not in stages:
            stages.append("execution")

    verification_outcome = ""
    if verification is not None and verification.outcome is not None:
        verification_outcome = verification.outcome.value
        passed = verification.outcome is VerificationOutcome.PASS
        steps.append(
            TraceStep(
                stage="verification",
                event_id=verification.id,
                ok=passed,
                detail=f"outcome={verification.outcome.value}",
            )
        )
        stages.append("verification")

    return ExecutionTrace(
        mission_id=state.mission_id,
        steps=steps[:MAX_TRACE_STEPS],
        stages=stages,
        proposed_count=proposed_count,
        executed_ids=executed_ids,
        failed_executions=failed_executions,
        verification_outcome=verification_outcome,
    )
