"""Public brain contracts (NomadicBrain Sprint 1 + Sprint 2 MAP core).

One import surface for the brain. Existing NomadicOS contracts are
re-exported as-is (never redefined): ``Goal``, ``Observation``,
``EvidenceItem``, ``VerificationResult``, ``FailureRecord``.

Sprint 1 invariant (no model, no execution)::

    goal = Goal.from_spec(...)
    state = BrainState.for_goal(goal, task_graph=TaskGraph.build(...))
    orchestrator = Orchestrator()
    state = orchestrator.start_task(state, ...)

Sprint 2 MAP core adds the cognitive proposal pipeline, all of it
model-agnostic behind the ``StructuredGenerator`` protocol::

    decomposer = ModelDecomposer(generator)      # goal   -> TaskGraph
    actor       = ModelActor(generator)          # subgoal -> candidates
    monitor     = ChainedMonitor(semantic=SemanticMonitor(generator))
    planner = MAPPlanner(ActorMonitorLoop())
    state, outcome = await planner.plan(state, decomposer, actor, monitor)

Proposals end at ``ActionCandidate`` + ``MonitorResult``: never an
``AuthorizedAction``, never execution, never authority.
"""

from nomadicos.brain.actor import Actor, ActorProposal, ModelActor
from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
)
from nomadicos.brain.decomposer import (
    Decomposer,
    Decomposition,
    ModelDecomposer,
    apply_decomposition,
)
from nomadicos.brain.errors import (
    BrainError,
    InvalidBrainState,
    InvalidTaskGraph,
    InvalidTransition,
    ModelOutputRejected,
)
from nomadicos.brain.events import (
    BRAIN_EVENT_SCHEMA_VERSION,
    BrainEvent,
    BrainEventType,
    EventRef,
)
from nomadicos.brain.loop import (
    ActorMonitorLoop,
    LoopConfig,
    MAPPlanner,
    PlanningFailure,
    PlanningOutcome,
    PlanningStage,
    PlanningStatus,
)
from nomadicos.brain.monitor import (
    ChainedMonitor,
    Monitor,
    SemanticMonitor,
    StructuralMonitor,
)
from nomadicos.brain.planner.orchestrator import Orchestrator
from nomadicos.brain.planner.task_graph import TaskGraph, TaskNode, TaskNodeStatus
from nomadicos.brain.schemas import (
    BRAIN_SCHEMA_VERSION,
    ActionCandidate,
    BudgetState,
    CandidateKind,
    Capability,
    Constraints,
    Decision,
    Hypothesis,
    MemoryRef,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    RecoveryPlan,
    Subgoal,
    WorkingMemoryView,
    WorldFact,
    WorldFactKind,
    WorldState,
    canonical_json,
)
from nomadicos.brain.state import BRAIN_STATE_SCHEMA_VERSION, BrainState
from nomadicos.contracts.core import FailureRecord, Goal
from nomadicos.contracts.execution import Observation
from nomadicos.contracts.verification import EvidenceItem, VerificationResult

__all__ = [
    "BRAIN_EVENT_SCHEMA_VERSION",
    "BRAIN_SCHEMA_VERSION",
    "BRAIN_STATE_SCHEMA_VERSION",
    "ActionCandidate",
    "Actor",
    "ActorMonitorLoop",
    "ActorProposal",
    "BrainError",
    "BrainEvent",
    "BrainEventType",
    "BrainState",
    "BudgetState",
    "CandidateKind",
    "Capability",
    "ChainedMonitor",
    "CognitiveRequest",
    "CognitiveRole",
    "Constraints",
    "Decision",
    "Decomposer",
    "Decomposition",
    "EventRef",
    "EvidenceItem",
    "FailureRecord",
    "Goal",
    "Hypothesis",
    "InvalidBrainState",
    "InvalidTaskGraph",
    "InvalidTransition",
    "LoopConfig",
    "MAPPlanner",
    "MemoryRef",
    "ModelActor",
    "ModelDecomposer",
    "ModelOutputRejected",
    "Monitor",
    "MonitorDecision",
    "MonitorResult",
    "MonitorStage",
    "Observation",
    "Orchestrator",
    "PlanningFailure",
    "PlanningOutcome",
    "PlanningStage",
    "PlanningStatus",
    "PlanningStage",
    "PlanningStatus",
    "RecoveryPlan",
    "StructuredGenerator",
    "Subgoal",
    "SemanticMonitor",
    "StructuralMonitor",
    "TaskGraph",
    "TaskNode",
    "TaskNodeStatus",
    "VerificationResult",
    "WorkingMemoryView",
    "WorldFact",
    "WorldFactKind",
    "WorldState",
    "apply_decomposition",
    "canonical_json",
]
