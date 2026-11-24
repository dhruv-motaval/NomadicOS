"""Brain-to-runtime adapter (Phase 1 vertical slice, SPEC §1, §19-21).

NomadicBrain proposes; NomadicOS authorizes and executes. This module is
the ONLY seam between the workflow graph and the Brain:

    TaskState (authoritative plan + observations)
        -> BrainState mirror (data only, mission_id == task_id)
        -> ActorMonitorLoop (Actor proposes, StructuralMonitor disposes)
        -> accepted ActionCandidate
        -> proposal JSON text -> parse_model_output (UNTRUSTED path)
        -> ActionProposal (system-supplied task/step/model/attempt)

Safety properties (fail closed):

- The Brain never sees an ``AuthorizedAction``, never executes, and
  cannot mint authority: accepted candidates re-enter the EXACT same
  deterministic parser (``parse_model_output``) as raw model text, so
  model-authored authority fields are rejected at any depth (SPEC §19)
  and correlation identity (task/step/model/attempt) stays
  system-supplied.
- The authoritative mission plan stays the workflow's plan; the Brain's
  TaskGraph is a bounded data MIRROR of that plan (one subgoal per
  step), never a second control loop.
- Candidate kinds the harness cannot execute (QUERY/EXPERIMENT) and
  malformed tool spellings fail closed as INVALID_PROPOSAL - never a
  silent fallback to raw model generation, never an execution.
- A WAIT candidate becomes only a completion CLAIM (§28): routing to
  the authoritative verifier, which alone may decide SUCCESS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nomadicos.action_ir.parser import ParsedProposal, parse_model_output
from nomadicos.brain.actor import ModelActor
from nomadicos.brain.adaptive import AdaptiveController
from nomadicos.brain.cognition import CognitiveRole, StructuredGenerator
from nomadicos.brain.deliberation import (
    AdaptiveSearchPolicy,
    DeliberationCoordinator,
    DeliberationOutcome,
    ModelEvaluator,
    ModelPredictor,
)
from nomadicos.brain.events import BrainEvent, BrainEventType
from nomadicos.brain.loop import ActorMonitorLoop, LoopConfig, PlanningOutcome, PlanningStatus
from nomadicos.brain.memory import MemoryController, MemoryRetriever
from nomadicos.brain.monitor import ChainedMonitor, Monitor, SemanticMonitor, StructuralMonitor
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.prediction_error import measure_prediction_error
from nomadicos.brain.schemas import (
    MAX_EVALUATIONS,
    MAX_MONITOR_RESULTS,
    MAX_PLAN_CANDIDATES,
    MAX_PREDICTIONS,
    ActionCandidate,
    CandidateKind,
    Subgoal,
    WorldFact,
    WorldFactKind,
    WorldState,
)
from nomadicos.brain.state import BRAIN_STATE_SCHEMA_VERSION, BrainState
from nomadicos.brain.uncertainty import apply_observation
from nomadicos.contracts.action import CompletionClaim, ProposalKind
from nomadicos.contracts.core import Goal, PlanStep
from nomadicos.contracts.execution import Observation
from nomadicos.inference.base import InferenceEngine
from nomadicos.inference.structured import EngineStructuredGenerator
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.orchestration.page_text import best_window
from nomadicos.tools.base import ToolRegistry

#: bounded feedback kept in the BrainState mirror (bounded memory, §14)
MAX_FED_OBSERVATIONS = 32
#: bounded canonical-JSON size for a persisted BrainState (corrupt or
#: runaway mirrors are rejected, never loaded)
MAX_BRAIN_STATE_JSON_CHARS = 262_144


def brain_state_for(
    goal: Goal,
    plan: list[PlanStep],
    *,
    task_id: str,
    observations: list | None = None,
) -> BrainState:
    """Initialize the BrainState for a mission from AUTHORITATIVE data.

    The brain task graph mirrors the workflow plan one-to-one (subgoal
    ``s{i}`` + node ``t{i}`` per step ``i``, linear dependencies); it is
    cognition DATA, not an independent control loop. Prior observations
    are replayed into the state so a reconstructed brain sees what the
    mission already observed.
    """
    subgoals = [
        Subgoal(id=f"s{i}", goal_id=goal.id, description=s.description) for i, s in enumerate(plan)
    ]
    # Node ids mirror the AUTHORITATIVE plan step ids one-to-one (the graph
    # mirrors the plan, never the other way around). The scheduler keys task
    # status by these ids; a divergent synthetic id space would orphan every
    # task in the status map.
    nodes = [
        TaskNode(
            id=s.id,
            description=s.description,
            subgoal_id=f"s{i}",
            depends_on=[plan[i - 1].id] if i else [],
        )
        for i, s in enumerate(plan)
    ]
    return BrainState(
        mission_id=task_id,
        goal=goal,
        task_graph=TaskGraph.build(goal_id=goal.id, nodes=nodes, subgoals=subgoals),
        observations=list(observations or []),
    )


#: newest event refs kept in a persisted mirror (the full capture lives in
#: TaskState.brain_events; history is only the reducer's recent tail)
MAX_PERSISTED_HISTORY = 256
#: BrainEvents kept in TaskState.brain_events for trace reconstruction
MAX_CAPTURED_EVENTS = 1500


def compact_brain_state(brain: BrainState) -> BrainState:
    """Keep only the newest entries of every growing list so a long mission
    never persists a mirror that fails its own bounds on reload (a failed
    load silently restarts cognition from scratch)."""
    caps = {
        "history": MAX_PERSISTED_HISTORY,
        "action_candidates": MAX_PLAN_CANDIDATES,
        "monitor_results": MAX_MONITOR_RESULTS,
        "planning_decisions": MAX_PLAN_CANDIDATES,
        "predictions": MAX_PREDICTIONS,
        "evaluations": MAX_EVALUATIONS,
    }
    trimmed = {
        name: getattr(brain, name)[-cap:]
        for name, cap in caps.items()
        if len(getattr(brain, name)) > cap
    }
    return brain.model_copy(update=trimmed) if trimmed else brain


def dump_brain_state(brain: BrainState) -> str:
    """Canonical serialization for graph/checkpoint state (data only)."""
    return compact_brain_state(brain).to_canonical_json()


def append_brain_events(
    previous: list[dict[str, Any]] | None, events: list[BrainEvent]
) -> list[dict[str, Any]]:
    """Bounded JSON capture of brain events, oldest first."""
    fresh = [e.model_dump(mode="json") for e in events]
    return [*(previous or []), *fresh][-MAX_CAPTURED_EVENTS:]


def load_brain_state(raw: str | None) -> BrainState | None:
    """Restore a BrainState from canonical JSON (fail closed).

    Corrupt, oversized, or schema-mismatched payloads yield None so the
    caller reconstructs from authoritative runtime data instead of
    trusting broken cognition state.
    """
    if not raw or not isinstance(raw, str):
        return None
    if len(raw) > MAX_BRAIN_STATE_JSON_CHARS:
        return None
    try:
        brain = BrainState.model_validate_json(raw)
    except Exception:
        return None
    if brain.schema_version != BRAIN_STATE_SCHEMA_VERSION:
        return None
    return brain


def feed_observation(brain: BrainState, observation: Observation) -> BrainState:
    """Append one execution observation to the BrainState (DATA only).

    Bounded: only the most recent observations are retained. Trust
    semantics are unchanged - an observation is evidence for later
    cognition, never authority and never success.
    """
    kept = [*brain.observations, observation][-MAX_FED_OBSERVATIONS:]
    return brain.model_copy(update={"observations": kept, "state_version": brain.state_version + 1})


#: characters of a fetch's output kept as the resolved fact's value
MAX_ANSWER_CHARS = 1000


def open_unknowns(brain: BrainState) -> list[str]:
    """Keys of facts still UNKNOWN (the brain's open information needs)."""
    return [k for k, f in brain.world.facts.items() if f.kind is WorldFactKind.UNKNOWN]


def needs_answered_by(brain: BrainState, candidate: ActionCandidate) -> list[str]:
    """Open unknowns this candidate says it reads (its key appears in what it
    expects or why it runs, as the actor prompt asks)."""
    text = f"{candidate.expected_effect} {candidate.rationale}"
    return [key for key in open_unknowns(brain) if key in text]


def _resolve_unknowns(brain: BrainState, keys: list[str], observation: Observation) -> BrainState:
    """A successful read turns the unknowns it targeted into OBSERVED facts
    (kind FACT, never VERIFIED) holding what it returned."""
    if str(observation.data.get("status", "")).upper() != "SUCCEEDED":
        return brain
    data = observation.data
    text = str(data.get("stdout_head") or data.get("content_head") or observation.summary)
    facts = dict(brain.world.facts)
    resolved = False
    for key in keys:
        fact = facts.get(key)
        if fact is not None and fact.kind is WorldFactKind.UNKNOWN:
            # an UNKNOWN fact's value is its question: keep the part that answers it
            answer = best_window(text, str(fact.value), MAX_ANSWER_CHARS)
            facts[key] = WorldFact(key=key, value=answer, kind=WorldFactKind.FACT)
            resolved = True
    if not resolved:
        return brain
    return brain.model_copy(
        update={
            "world": WorldState(version=brain.world.version + 1, facts=facts),
            "state_version": brain.state_version + 1,
        }
    )


def observe_into_brain(
    brain: BrainState,
    observation: Observation,
    *,
    selected_prediction: str | None = None,
    update_beliefs: bool = False,
    answers: list[str] | None = None,
) -> tuple[BrainState, list[BrainEvent]]:
    """Feed one REAL execution observation back into the brain (data only):
    always the bounded observation list; with ``update_beliefs`` its data
    also becomes observed world facts; and when the executed action was a
    brain-selected candidate with a prediction, the prediction error is
    measured (by its prediction id) and recorded as an event."""
    brain = feed_observation(brain, observation)
    if update_beliefs:
        brain = apply_observation(brain, observation)
    if answers:
        brain = _resolve_unknowns(brain, answers, observation)
    if not selected_prediction:
        return brain, []
    brain, error = measure_prediction_error(brain, selected_prediction, observation)
    if error is None:
        return brain, []
    event = BrainEvent(
        mission_id=brain.mission_id,
        timestamp=datetime.now(UTC),
        event_type=BrainEventType.BRAIN_PREDICTION_ERROR_MEASURED,
        actor="prediction_error",
        parent_event_id=brain.history[-1].event_id if brain.history else None,
        payload=error.model_dump(mode="json"),
    )
    return brain.with_event(event), [event]


def without_empty_optional_args(candidate: ActionCandidate, tools: ToolRegistry) -> ActionCandidate:
    """Drop optional arguments the model filled with an empty string or null.

    Small models fill every key they are shown; an empty optional means "unset".
    Required keys are never touched (an empty ``content`` is a real value), and the
    tool's own argument validation still decides everything afterwards.
    """
    tool_name, _, operation = (candidate.capability or candidate.tool).partition(".")
    impl = tools.find(tool_name)
    model = impl.ops.get(operation) if impl is not None else None
    if model is None:
        return candidate
    kept = {
        k: v
        for k, v in candidate.arguments.items()
        if v not in ("", None) or (k in model.model_fields and model.model_fields[k].is_required())
    }
    return candidate.model_copy(update={"arguments": kept})


def candidate_to_model_output(candidate: ActionCandidate) -> str:
    """Deterministically serialize an accepted candidate into the SAME
    untrusted text surface raw model output takes (SPEC §19).

    The result is parsed by ``parse_model_output`` like any model reply,
    so authority-shaped fields anywhere in the candidate (including
    inside ``arguments``) are rejected by the existing parser and
    correlation identity stays system-supplied. Tool spellings without
    an operation segment fail closed.
    """
    if candidate.kind is CandidateKind.WAIT:
        raise InvalidProposal("WAIT candidates are claims, not proposals")
    # the actor prompt makes `tool` the part before the dot; `capability` is "<tool>.<operation>"
    tool = candidate.tool if "." in candidate.tool else (candidate.capability or candidate.tool)
    if not tool or "." not in tool:
        raise InvalidProposal(
            f"brain candidate {candidate.id!r} tool {tool!r} carries no operation segment"
        )
    prefix, _, operation = tool.partition(".")
    raw = json.dumps(
        {
            "tool": prefix,
            "operation": operation,
            "args": dict(candidate.arguments),
            "note": candidate.rationale,
        }
    )
    return raw


def candidate_to_parsed(
    candidate: ActionCandidate, *, task_id: str, step_id: str, model_id: str, attempt: int
) -> ParsedProposal:
    """Convert one accepted candidate through the UNTRUSTED parser path.

    Mapping (fail closed, no invention):
    - EXECUTE -> ACTION proposal;
    - OBSERVE -> OBSERVE-kind proposal (same pipeline, semantic label only);
    - WAIT    -> completion claim (verify_goal still holds SUCCESS authority);
    - QUERY / EXPERIMENT -> rejected (not executable by the harness yet).
    """
    if candidate.kind is CandidateKind.WAIT:
        return ParsedProposal(
            CompletionClaim(
                task_id=task_id,
                model_id=model_id,
                claim=True,
                justification=candidate.rationale,
            )
        )
    text = candidate_to_model_output(candidate)
    parsed = parse_model_output(
        text,
        task_id=task_id,
        step_id=step_id,
        model_id=model_id,
        attempt=attempt,
    )
    if candidate.kind is CandidateKind.OBSERVE and not parsed.is_claim:
        proposal = parsed.value.model_copy(update={"kind": ProposalKind.OBSERVE})
        return ParsedProposal(proposal)
    return parsed


async def brain_propose_for_step(
    brain: BrainState,
    subgoal: Subgoal,
    *,
    engine: InferenceEngine,
    model_id: str,
    loop: ActorMonitorLoop | None = None,
) -> tuple[BrainState, PlanningOutcome]:
    """Run ONE bounded Actor<->Monitor pass for the current step.

    Deterministic brain pipeline (Phase 2): ModelActor proposes candidates
    through the ``EngineStructuredGenerator`` seam (the ONLY model path);
    the ChainedMonitor disposes every candidate with explicit verdicts -
    the deterministic StructuralMonitor FIRST, the model-backed
    SemanticMonitor only for structurally-accepted candidates. The
    semantic stage is fail-closed: malformed monitor output rejects every
    candidate under evaluation (no default-accept path exists), and a
    monitor transport failure raises out of the loop, which the caller
    converts into a typed failure - it can NEVER bypass validation,
    authorization, or execution.

    Bounded by construction: ``LoopConfig.max_iterations`` caps the
    actor<->monitor refinement pairs, so nested model calls per pass are
    finite (max_iterations x (1 actor + <=1 semantic monitor) calls).
    The semantic monitor shares the actor's model - it is an additional
    quality gate, never independent proof (SPEC §56.9: model claims are
    not verification).
    """
    runner = loop if loop is not None else ActorMonitorLoop(LoopConfig())
    generator = EngineStructuredGenerator(engine, model_id, temperature=0.0)
    monitor: Monitor = ChainedMonitor(StructuralMonitor(), SemanticMonitor(generator))
    return await runner.run(brain, subgoal, ModelActor(generator), monitor)


@dataclass
class BrainStep:
    """Everything one brain step produced (data only)."""

    brain: BrainState
    outcome: PlanningOutcome
    #: the candidate to run: deliberation's selection, else the first accepted
    candidate: ActionCandidate | None
    #: prediction deliberation selected ``candidate`` by (None when the
    #: first accepted candidate stands): what OBSERVE scores reality against
    prediction_id: str | None = None
    events: list[BrainEvent] = field(default_factory=list)
    deliberation: DeliberationOutcome | None = None


async def brain_step(
    brain: BrainState,
    subgoal: Subgoal,
    *,
    engine: InferenceEngine,
    model_id: str,
    adaptive: bool = False,
    memory: MemoryRetriever | None = None,
    deliberate: bool = False,
    search_depth: int = 1,
    role_generators: dict[CognitiveRole, StructuredGenerator] | None = None,
) -> BrainStep:
    """One full brain step for the current subgoal, every stage bounded:

    adaptive pass (memory -> attention -> beliefs -> metacognition ->
    information needs -> effort) -> Actor<->Monitor loop -> deliberation
    (predict -> evaluate -> bounded search -> deterministic selection).

    The actor always uses the step's routed ``model_id`` (escalation keeps
    working); ``role_generators`` may serve monitor/predictor/evaluator.
    Adaptive data and deliberation only CHOOSE among monitor-accepted
    candidates; the result still goes through the untrusted parser,
    validation, authority and the executor. Deliberation failure is
    fail-safe: the first accepted candidate is kept and the failure stays
    in the captured events.
    """
    events: list[BrainEvent] = []
    generator = EngineStructuredGenerator(engine, model_id, temperature=0.0)
    roles = role_generators or {}
    brain = brain.model_copy(update={"active_subgoal": subgoal})
    loop_config = LoopConfig()
    memory_notes: list[str] = []
    if adaptive:
        controller = MemoryController(memory) if memory is not None else None
        brain = AdaptiveController(memory=controller, events=events).run(brain)
        if controller is not None:
            try:
                memory_notes = [
                    text
                    for hit in controller.retrieve_for_state(brain)
                    if (text := getattr(hit.record, "content", "") or "").strip()
                ]
            except Exception:  # noqa: BLE001 - memory is fail-safe by contract
                memory_notes = []
        if brain.effort_plan is not None:
            # effort can lower the per-step model budget, never raise it
            loop_config = LoopConfig(
                max_iterations=min(loop_config.max_iterations, brain.effort_plan.max_iterations)
            )
            search_depth = min(search_depth, brain.effort_plan.search_depth)
    actor = ModelActor(generator, memory_notes=memory_notes)
    monitor: Monitor = ChainedMonitor(
        StructuralMonitor(), SemanticMonitor(roles.get(CognitiveRole.MONITOR, generator))
    )
    parent = brain.history[-1].event_id if brain.history else None
    brain, outcome = await ActorMonitorLoop(loop_config, events=events).run(
        brain, subgoal, actor, monitor, parent_event_id=parent
    )
    candidate = first_accepted_candidate(brain, outcome)
    deliberation: DeliberationOutcome | None = None
    prediction_id: str | None = None
    if deliberate and candidate is not None:
        coordinator = DeliberationCoordinator(
            actor,
            monitor,
            ModelPredictor(roles.get(CognitiveRole.PREDICTOR, generator)),
            ModelEvaluator(roles.get(CognitiveRole.EVALUATOR, generator)),
            events=events,
        )
        try:
            brain, deliberation = await coordinator.deliberate(
                brain,
                subgoal,
                outcome,
                parent_event_id=brain.history[-1].event_id if brain.history else None,
                policy=AdaptiveSearchPolicy(max_depth=search_depth),
            )
        except Exception:  # noqa: BLE001 - deliberation advises; the accepted candidate stands
            deliberation = None
        if deliberation is not None and deliberation.decision is not None:
            chosen = deliberation.decision.candidate_id
            selected = next((c for c in reversed(brain.action_candidates) if c.id == chosen), None)
            if selected is not None and chosen in outcome.accepted_candidate_ids:
                candidate = selected
                node_id = deliberation.decision.factors.get("node_id")
                prediction_id = next(
                    (n.prediction_id for n in deliberation.search.nodes if n.node_id == node_id),
                    None,
                )
    return BrainStep(
        brain=brain,
        outcome=outcome,
        candidate=candidate,
        prediction_id=prediction_id,
        events=events,
        deliberation=deliberation,
    )


def first_accepted_candidate(brain: BrainState, outcome: PlanningOutcome) -> ActionCandidate | None:
    """The FIRST accepted candidate of the outcome (deterministic order).

    Extra accepted candidates stay in the BrainState as data; one
    proposal per propose pass matches the graph's single-proposal flow.
    """
    if outcome.status is not PlanningStatus.ACCEPTED or not outcome.accepted_candidate_ids:
        return None
    accepted_id = outcome.accepted_candidate_ids[0]
    for candidate in reversed(brain.action_candidates):
        if candidate.id == accepted_id:
            return candidate
    return None


__all__ = [
    "BrainStep",
    "append_brain_events",
    "brain_propose_for_step",
    "brain_step",
    "compact_brain_state",
    "brain_state_for",
    "candidate_to_parsed",
    "without_empty_optional_args",
    "candidate_to_model_output",
    "dump_brain_state",
    "feed_observation",
    "first_accepted_candidate",
    "needs_answered_by",
    "observe_into_brain",
    "open_unknowns",
    "load_brain_state",
]
