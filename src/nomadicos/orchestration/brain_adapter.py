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

from nomadicos.action_ir.parser import ParsedProposal, parse_model_output
from nomadicos.brain.actor import ModelActor
from nomadicos.brain.loop import ActorMonitorLoop, LoopConfig, PlanningOutcome, PlanningStatus
from nomadicos.brain.monitor import ChainedMonitor, Monitor, SemanticMonitor, StructuralMonitor
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode
from nomadicos.brain.schemas import ActionCandidate, CandidateKind, Subgoal
from nomadicos.brain.state import BRAIN_STATE_SCHEMA_VERSION, BrainState
from nomadicos.contracts.action import CompletionClaim, ProposalKind
from nomadicos.contracts.core import Goal, PlanStep
from nomadicos.contracts.execution import Observation
from nomadicos.inference.base import InferenceEngine
from nomadicos.inference.structured import EngineStructuredGenerator
from nomadicos.kernel.errors import InvalidProposal

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


def dump_brain_state(brain: BrainState) -> str:
    """Canonical serialization for graph/checkpoint state (data only)."""
    return brain.to_canonical_json()


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
    tool = candidate.tool or candidate.capability
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
    "brain_propose_for_step",
    "brain_state_for",
    "candidate_to_parsed",
    "candidate_to_model_output",
    "dump_brain_state",
    "feed_observation",
    "first_accepted_candidate",
    "load_brain_state",
]
