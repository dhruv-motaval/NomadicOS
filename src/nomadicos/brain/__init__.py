"""NomadicBrain — deterministic cognitive substrate (Sprint 1 + Sprint 2).

Sprint 1 invariant (no model, no tool execution)::

    Goal -> BrainState -> TaskGraph -> Deterministic Orchestrator

Sprint 2 MAP core adds the cognitive proposal pipeline, model-agnostic
behind the ``StructuredGenerator`` protocol (spec §1.1)::

    Goal -> Decomposer -> TaskGraph/Subgoals
         -> Actor      -> ActionCandidates
         -> Monitor    -> accepted / rejected + feedback
         -> bounded Actor<->Monitor refinement -> PlanningOutcome

The brain is structure above models: it proposes and represents; it
never bypasses the NomadicOS security/runtime boundaries — Action IR
(§19), owner authority (§4-5), executor authorization (§20-21), and
goal verification semantics (§27-29) remain untouched and authoritative.
No Phase 2 component executes tools, grants authority, or mutates
policy; the pipeline stops at validated proposals and monitor decisions.

The authoritative implementation specification lives at
``src/nomadicos/brain/NomadicBrain_MASTER_IMPLEMENTATION_SPEC.md``.
"""

from nomadicos.brain.contracts import (
    BRAIN_EVENT_SCHEMA_VERSION,
    BRAIN_SCHEMA_VERSION,
    BRAIN_STATE_SCHEMA_VERSION,
    ActionCandidate,
    Actor,
    ActorMonitorLoop,
    ActorProposal,
    BrainError,
    BrainEvent,
    BrainEventType,
    BrainState,
    BudgetState,
    CandidateKind,
    Capability,
    ChainedMonitor,
    CognitiveRequest,
    CognitiveRole,
    Constraints,
    Decision,
    Decomposer,
    Decomposition,
    EventRef,
    EvidenceItem,
    FailureRecord,
    Goal,
    Hypothesis,
    InvalidBrainState,
    InvalidTaskGraph,
    InvalidTransition,
    LoopConfig,
    MAPPlanner,
    MemoryRef,
    ModelActor,
    ModelDecomposer,
    ModelOutputRejected,
    Monitor,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    Observation,
    Orchestrator,
    PlanningFailure,
    PlanningOutcome,
    PlanningStage,
    PlanningStatus,
    RecoveryPlan,
    StructuredGenerator,
    Subgoal,
    TaskGraph,
    TaskNode,
    TaskNodeStatus,
    VerificationResult,
    WorkingMemoryView,
    WorldFact,
    WorldFactKind,
    WorldState,
    apply_decomposition,
    canonical_json,
)

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
    "RecoveryPlan",
    "StructuredGenerator",
    "Subgoal",
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
