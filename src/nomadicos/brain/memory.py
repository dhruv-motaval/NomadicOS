"""Brain memory seam (Sprint 5, NomadicBrain spec §10-11) — ONE controller
boundary between cognition and the memory brick.

The brain never imports the memory implementation (import boundary, Sprint
2): exactly like the model seam (``StructuredGenerator``), memory arrives
behind a narrow Protocol (satisfied structurally by the memory brick's
deterministic Retriever):

    MemoryRetriever.retrieve(query) -> list[RetrievedMemory]

``MemoryController`` is the single goal-aware retrieval boundary:

    Current Goal (BrainState)
        → build_query (goal + active subgoal + optional extra, bounded)
        → retriever (semantic + episodic + procedural, graph expansion)
        → rerank + relevance filter (min_score) + bounded top-K
        → BrainState.relevant_memories (refs only — memory is context)

DATA only: a retrieved score is relevance data, never truth, verification,
or permission. The controller mutates nothing but the returned NEW
BrainState instance (frozen transitions); it cannot authorize, execute, or
mark anything as an outcome. Retrieval is deterministic given a
deterministic retriever; latency/hit metrics are measurement-only and
never enter replayable state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.schemas import MemoryRef, WorkingMemoryView
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract
from nomadicos.contracts.memory import (
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    RetrievedMemory,
    TrajectoryStep,
    TrajectoryStepKind,
)

QUERY_TEXT_CHARS = 400
_OBS_SUMMARY_CHARS = 160
_CANDIDATE_SUMMARY_CHARS = 160


@runtime_checkable
class MemoryRetriever(Protocol):
    """The ONLY memory seam the brain depends on (structural protocol —
    satisfied by the memory brick's deterministic Retriever)."""

    def retrieve(self, query: MemoryQuery) -> list[RetrievedMemory]: ...


class RetrievalPolicy(Contract):
    """Deterministic retrieval parameters (hard bounds, model cannot change)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: relevance filter: hits below this score are dropped
    min_score: float = Field(default=0.15, ge=0.0, le=1.0)
    #: bounded top-K
    top_k: int = Field(default=6, ge=1, le=32)
    #: which durable kinds feed the goal-aware view (must be non-empty:
    #: an empty filter would mean "everything" — a firehose is forbidden)
    kinds: list[MemoryKind] = Field(
        default_factory=lambda: [
            MemoryKind.SEMANTIC,
            MemoryKind.EPISODIC,
            MemoryKind.PROCEDURAL,
        ]
    )

    @model_validator(mode="after")
    def _kinds_nonempty(self) -> RetrievalPolicy:
        if not self.kinds:
            raise ValueError("retrieval policy requires at least one memory kind")
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("retrieval policy kinds must be distinct")
        return self


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """One goal-aware retrieval measurement (Sprint 10 feed; DATA only).

    Latency is measurement-only and never enters replayable state."""

    latency_ms: float
    returned: int
    superseded_returned: int
    duplicates: int
    top_score: float
    avg_score: float
    hit: bool


class MemoryController:
    """One clear goal-aware memory boundary feeding BrainState."""

    def __init__(self, retriever: MemoryRetriever, policy: RetrievalPolicy | None = None) -> None:
        self._retriever = retriever
        self._policy = policy if policy is not None else RetrievalPolicy()
        self._retrievals = 0
        self._hits = 0
        self.last_metrics: RetrievalMetrics | None = None

    # ------------------------------------------------------------ query ---
    def build_query(self, state: BrainState, *, extra_query: str = "") -> MemoryQuery:
        """Deterministic query from the CURRENT goal (+ active subgoal)."""
        parts = [state.goal.objective]
        if state.active_subgoal is not None:
            parts.append(state.active_subgoal.description)
        if extra_query.strip():
            parts.append(extra_query.strip())
        return MemoryQuery(
            text=" ".join(p for p in parts if p)[:QUERY_TEXT_CHARS],
            kinds=list(self._policy.kinds),
            limit=self._policy.top_k,
            min_score=self._policy.min_score,
        )

    # -------------------------------------------------------- retrieval ---
    def retrieve_for_state(
        self, state: BrainState, *, extra_query: str = ""
    ) -> list[RetrievedMemory]:
        """Goal-aware retrieval: query → retrieve → rerank → relevance
        filter (policy min_score, enforced HERE regardless of retriever
        behavior) → bounded top-K."""
        query = self.build_query(state, extra_query=extra_query)
        start = time.perf_counter()
        hits = [
            hit for hit in self._retriever.retrieve(query) if hit.score >= self._policy.min_score
        ][: self._policy.top_k]
        latency_ms = (time.perf_counter() - start) * 1000.0
        self._retrievals += 1
        if hits:
            self._hits += 1
        scores = [h.score for h in hits]
        contents = [h.record.content for h in hits if isinstance(h.record, MemoryRecord)]
        self.last_metrics = RetrievalMetrics(
            latency_ms=round(latency_ms, 3),
            returned=len(hits),
            superseded_returned=sum(
                1 for h in hits if isinstance(h.record, MemoryRecord) and h.record.superseded
            ),
            duplicates=len(contents) - len(set(contents)),
            top_score=max(scores) if scores else 0.0,
            avg_score=round(sum(scores) / len(scores), 4) if scores else 0.0,
            hit=bool(hits),
        )
        return hits

    def into_state(
        self,
        state: BrainState,
        *,
        extra_query: str = "",
        working_view: WorkingMemoryView | None = None,
    ) -> BrainState:
        """Populate ``relevant_memories`` (refs only) on a NEW BrainState.

        The input state is never mutated. Scores are relevance data; the
        retrieval itself is measured via ``last_metrics``."""
        hits = self.retrieve_for_state(state, extra_query=extra_query)
        refs = [MemoryRef(memory_id=h.record.id, score=h.score) for h in hits]
        update: dict[str, object] = {
            "relevant_memories": refs,
            "state_version": state.state_version + 1,
        }
        if working_view is not None:
            update["working_memory"] = working_view
        return state.model_copy(update=update)

    # ---------------------------------------------------------- metrics ---
    def hit_rate(self) -> float:
        """Fraction of retrievals that returned at least one memory."""
        if self._retrievals == 0:
            return 0.0
        return round(self._hits / self._retrievals, 4)


def trajectory_steps(state: BrainState) -> list[TrajectoryStep]:
    """Deterministic trajectory steps from BrainState (actions, then
    observations, then history event refs) — structured, ref-backed."""
    steps: list[TrajectoryStep] = []
    for candidate in state.action_candidates:
        summary = candidate.tool or candidate.kind.value
        steps.append(
            TrajectoryStep(
                kind=TrajectoryStepKind.ACTION,
                ref=candidate.id,
                summary=summary[:_CANDIDATE_SUMMARY_CHARS],
            )
        )
    for observation in state.observations:
        steps.append(
            TrajectoryStep(
                kind=TrajectoryStepKind.OBSERVATION,
                ref=observation.id,
                summary=observation.summary[:_OBS_SUMMARY_CHARS],
            )
        )
    for ref in state.history:
        steps.append(TrajectoryStep(kind=TrajectoryStepKind.OUTCOME, ref=ref.event_id))
    return steps
