"""Monitor — two-stage candidate validation (NomadicBrain spec §15,
Sprint 2).

Purpose: *is the proposed action valid for the current state?*

1. ``StructuralMonitor`` — DETERMINISTIC and model-independent: schema,
   candidate identity, subgoal binding, capability existence (against
   the state's declared capabilities), bounded arguments. It can reject
   malformed candidates with no model involved, ever.
2. ``SemanticMonitor`` — model-backed consistency/relevance checks,
   reachable ONLY through the ``StructuredGenerator`` protocol. Its
   output is typed and fail closed: malformed output can never approve
   anything; candidates without an explicit verdict are rejected.

``ChainedMonitor`` composes both: structural rejects short-circuit;
semantic sees only structurally-valid candidates. Neither stage
authorizes anything — a MonitorResult is DATA, and execution still
flows exclusively through Action IR -> authority -> executor.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from nomadicos.brain.actor import ActorProposal
from nomadicos.brain.cognition import (
    CognitiveRequest,
    CognitiveRole,
    StructuredGenerator,
    parse_model_json,
)
from nomadicos.brain.decomposer import _model_output_rejected
from nomadicos.brain.planner.task_graph import is_safe_node_id
from nomadicos.brain.schemas import (
    MAX_ARGUMENTS_JSON_CHARS,
    MAX_ITEM_CHARS,
    ActionCandidate,
    CandidateKind,
    MonitorDecision,
    MonitorResult,
    MonitorStage,
    Subgoal,
)
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Contract

MAX_VERDICTS = 16


@runtime_checkable
class Monitor(Protocol):
    """Model-independent monitor seam (spec §15): explicit verdicts for
    every proposed candidate, never implicit approval."""

    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal: ActorProposal,
        iteration: int,
    ) -> list[MonitorResult]: ...


class StructuralMonitor:
    """Deterministic structural validation (spec §15 deterministic stage).

    Pure function of (state, subgoal, proposal): no model, no I/O, no
    ids or timestamps. Any violation yields an explicit REJECTED result
    with a structured reason; every candidate gets exactly one verdict.
    """

    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal: ActorProposal,
        iteration: int,
    ) -> list[MonitorResult]:
        results: list[MonitorResult] = []
        seen_ids: set[str] = set()
        declared = {cap.name for cap in state.capabilities}
        for candidate in proposal.candidates:
            result = self._evaluate_one(
                candidate,
                subgoal=subgoal,
                iteration=iteration,
                seen_ids=seen_ids,
                declared_capabilities=declared,
            )
            seen_ids.add(candidate.id)
            results.append(result)
        return results

    def _evaluate_one(
        self,
        candidate: ActionCandidate,
        *,
        subgoal: Subgoal,
        iteration: int,
        seen_ids: set[str],
        declared_capabilities: set[str],
    ) -> MonitorResult:
        def reject(reason: str) -> MonitorResult:
            return MonitorResult(
                candidate_id=candidate.id,
                decision=MonitorDecision.REJECTED,
                stage=MonitorStage.STRUCTURAL,
                reasons=[reason],
                iteration=iteration,
            )

        if not is_safe_node_id(candidate.id):
            return reject("STRUCTURAL SCHEMA: unsafe candidate id")
        if candidate.id in seen_ids:
            return reject("STRUCTURAL DUPLICATE: candidate id already proposed")
        if candidate.subgoal_id != subgoal.id:
            return reject(
                f"STRUCTURAL BINDING: candidate targets subgoal "
                f"{candidate.subgoal_id!r}, not the active {subgoal.id!r}"
            )
        if candidate.kind is CandidateKind.EXECUTE and not (
            candidate.tool.strip() or candidate.capability.strip()
        ):
            return reject("STRUCTURAL SCHEMA: EXECUTE candidate lacks a tool or capability")
        if not candidate.expected_effect.strip():
            return reject("STRUCTURAL SCHEMA: expected_effect must not be empty")
        for pre in candidate.preconditions:
            if not pre.strip() or len(pre) > MAX_ITEM_CHARS:
                return reject("STRUCTURAL SCHEMA: preconditions must be non-empty and bounded")
        if (
            candidate.capability
            and declared_capabilities
            and (candidate.capability not in declared_capabilities)
        ):
            return reject(
                f"STRUCTURAL CAPABILITY: {candidate.capability!r} is not declared by "
                "the mission state"
            )
        try:
            arguments_json = json.dumps(candidate.arguments, sort_keys=True)
        except (TypeError, ValueError):
            return reject("STRUCTURAL SCHEMA: arguments are not JSON-serializable")
        if len(arguments_json) > MAX_ARGUMENTS_JSON_CHARS:
            return reject("STRUCTURAL SCHEMA: arguments exceed the size bound")
        return MonitorResult(
            candidate_id=candidate.id,
            decision=MonitorDecision.ACCEPTED,
            stage=MonitorStage.STRUCTURAL,
            reasons=[],
            iteration=iteration,
        )


class SemanticVerdict(Contract):
    """One model-proposed semantic verdict (typed, closed schema)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    accepted: bool
    reason: str = ""

    @model_validator(mode="after")
    def _coherent(self) -> SemanticVerdict:
        if not is_safe_node_id(self.candidate_id):
            raise ValueError(f"unsafe candidate reference {self.candidate_id!r}")
        if len(self.reason) > MAX_ITEM_CHARS:
            raise ValueError(f"verdict reason exceeds {MAX_ITEM_CHARS} chars")
        return self


class SemanticMonitorOutput(Contract):
    """The ONLY shape model text may take as semantic monitoring output.

    Explicit decisions only: there is no default-accept path. Verdicts
    that do not reference a known candidate are dropped, never applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdicts: list[SemanticVerdict] = Field(min_length=1, max_length=MAX_VERDICTS)


class SemanticMonitor:
    """Model-backed semantic validation (spec §15 semantic stage).

    Reachable only via the ``StructuredGenerator`` protocol. Malformed
    output rejects ALL candidates under evaluation (fail closed, no
    implicit approval); the deterministic structural layer stays
    authoritative and is never replaced by this model check.
    """

    def __init__(self, generator: StructuredGenerator) -> None:
        self._generator = generator

    async def evaluate_candidates(
        self,
        state: BrainState,
        subgoal: Subgoal,
        candidates: Sequence[ActionCandidate],
        iteration: int,
    ) -> list[MonitorResult]:
        request = CognitiveRequest(
            role=CognitiveRole.MONITOR,
            mission_id=state.mission_id,
            payload=self._request_payload(state, subgoal, candidates),
        )
        text = await self._generator.generate_structured(request)
        return self._apply_output(text, subgoal=subgoal, candidates=candidates, iteration=iteration)

    def _request_payload(
        self,
        state: BrainState,
        subgoal: Subgoal,
        candidates: Sequence[ActionCandidate],
    ) -> dict[str, object]:
        return {
            "subgoal": {"id": subgoal.id, "description": subgoal.description},
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "world_facts": {
                key: {"value": fact.value, "kind": fact.kind.value}
                for key, fact in list(state.world.facts.items())[:16]
            },
            "constraints": list(state.constraints.items),
        }

    def _apply_output(
        self,
        text: str,
        *,
        subgoal: Subgoal,
        candidates: Sequence[ActionCandidate],
        iteration: int,
    ) -> list[MonitorResult]:
        try:
            payload = parse_model_json(text, context="semantic monitor")
            parsed = SemanticMonitorOutput.model_validate(payload)
        except Exception as exc:  # fail closed: any malformed output rejects everything
            detail = _model_output_rejected("semantic monitor", exc).message
            return [
                MonitorResult(
                    candidate_id=c.id,
                    decision=MonitorDecision.REJECTED,
                    stage=MonitorStage.SEMANTIC,
                    reasons=[f"SEMANTIC MALFORMED: monitor output rejected ({detail})"],
                    iteration=iteration,
                )
                for c in candidates
            ]

        verdicts_by_id: dict[str, SemanticVerdict] = {}
        for entry in parsed.verdicts:
            if entry.candidate_id not in {c.id for c in candidates}:
                continue  # unknown references are dropped, never applied
            verdicts_by_id.setdefault(entry.candidate_id, entry)  # first wins

        results: list[MonitorResult] = []
        for candidate in candidates:
            verdict: SemanticVerdict | None = verdicts_by_id.get(candidate.id)
            if verdict is None:
                results.append(
                    MonitorResult(
                        candidate_id=candidate.id,
                        decision=MonitorDecision.REJECTED,
                        stage=MonitorStage.SEMANTIC,
                        reasons=["SEMANTIC MISSING: no verdict for candidate (fail closed)"],
                        iteration=iteration,
                    )
                )
            elif verdict.accepted:
                results.append(
                    MonitorResult(
                        candidate_id=candidate.id,
                        decision=MonitorDecision.ACCEPTED,
                        stage=MonitorStage.SEMANTIC,
                        reasons=[verdict.reason] if verdict.reason else [],
                        iteration=iteration,
                    )
                )
            else:
                rejection_reason = (
                    f"SEMANTIC: {verdict.reason}" if verdict.reason else "SEMANTIC: rejected"
                )
                results.append(
                    MonitorResult(
                        candidate_id=candidate.id,
                        decision=MonitorDecision.REJECTED,
                        stage=MonitorStage.SEMANTIC,
                        reasons=[rejection_reason],
                        iteration=iteration,
                    )
                )
        return results


class ChainedMonitor:
    """Structural stage first, semantic stage second (spec §15).

    The deterministic layer is never bypassed: only structurally-accepted
    candidates reach the model-backed semantic stage. Results preserve
    the proposal's candidate order.
    """

    def __init__(
        self,
        structural: StructuralMonitor | None = None,
        semantic: SemanticMonitor | None = None,
    ) -> None:
        self._structural = structural if structural is not None else StructuralMonitor()
        self._semantic = semantic

    async def evaluate(
        self,
        state: BrainState,
        subgoal: Subgoal,
        proposal: ActorProposal,
        iteration: int,
    ) -> list[MonitorResult]:
        structural_results = await self._structural.evaluate(state, subgoal, proposal, iteration)
        accepted = [
            candidate
            for candidate, result in zip(proposal.candidates, structural_results, strict=True)
            if result.decision is MonitorDecision.ACCEPTED
        ]
        if self._semantic is None or not accepted:
            return structural_results
        semantic_results = await self._semantic.evaluate_candidates(
            state, subgoal, accepted, iteration
        )
        semantic_by_id = {r.candidate_id: r for r in semantic_results}
        merged: list[MonitorResult] = []
        for candidate, structural_result in zip(
            proposal.candidates, structural_results, strict=True
        ):
            if structural_result.decision is MonitorDecision.REJECTED:
                merged.append(structural_result)
            else:
                merged.append(semantic_by_id[candidate.id])
        return merged
