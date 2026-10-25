"""Skill retrieval (Sprint 8): deterministic capability → skill ranking.

    Goal → required capability → skill retrieval → deterministic ranking
         → bounded candidates

Ranking uses ONLY information the repository actually has: capability
match (required), availability (enabled + healthy), role compatibility,
prerequisite satisfaction, input compatibility, measured evidence
(success rate from recorded evaluation — never invented), and risk
ceiling. Cold-start (unmeasured) candidates rank after measured ones at
the same capability. Ordering (-score, skill_id, -version) is fully
deterministic; output bounded by the query. Exclusions are auditable
(never silent).
"""

from __future__ import annotations

from nomadicos.contracts.skills import (
    MAX_SKILL_MATCH_REASONS,
    SkillDefinition,
    SkillMatch,
    SkillQuery,
    SkillRetrieval,
)
from nomadicos.skills.registry import SkillRegistry

#: capability match contribution (required for candidacy)
_CAPABILITY_SCORE = 0.5
#: measured-evidence contribution (0.0 when unmeasured — cold start)
_EVIDENCE_SCORE = 0.5


def _input_compatible(definition: SkillDefinition, provided: list[str]) -> bool:
    """All REQUIRED inputs must be coverable by the caller's args."""
    provided_set = set(provided)
    return all((not param.required) or param.name in provided_set for param in definition.inputs)


def _prerequisites_met(definition: SkillDefinition, satisfied: list[str]) -> bool:
    return set(definition.prerequisites) <= set(satisfied)


def _role_allowed(definition: SkillDefinition, role: str) -> bool:
    return not definition.supported_roles or role in definition.supported_roles


def rank_skills(registry: SkillRegistry, query: SkillQuery) -> SkillRetrieval:
    """Deterministic bounded skill retrieval for one capability request."""
    matches: list[tuple[float, bool, SkillDefinition, list[str]]] = []
    excluded: list[str] = []
    for definition in registry.by_capability(query.capability):
        reasons: list[str] = []
        if not definition.enabled:
            excluded.append(f"{definition.skill_id}: disabled")
            continue
        if not definition.healthy:
            excluded.append(f"{definition.skill_id}: unhealthy")
            continue
        if not _role_allowed(definition, query.role):
            excluded.append(
                f"{definition.skill_id}: role {query.role!r} not in {definition.supported_roles}"
            )
            continue
        if definition.risk.rank > query.max_risk.rank:
            excluded.append(
                f"{definition.skill_id}: risk {definition.risk.value} "
                f"above ceiling {query.max_risk.value}"
            )
            continue
        if not _prerequisites_met(definition, query.satisfied_prerequisites):
            missing = sorted(set(definition.prerequisites) - set(query.satisfied_prerequisites))
            excluded.append(f"{definition.skill_id}: prerequisites missing {missing}")
            continue
        if not _input_compatible(definition, query.provided_args):
            required = {p.name for p in definition.inputs if p.required}
            provided = set(query.provided_args)
            excluded.append(
                f"{definition.skill_id}: required inputs {sorted(required - provided)} not provided"
            )
            continue
        measured = definition.evidence.samples > 0
        quality = definition.evidence.success_rate if measured else 0.0
        score = round(_CAPABILITY_SCORE + _EVIDENCE_SCORE * (quality or 0.0), 4)
        reasons = [f"capability_match={query.capability}"]
        if measured:
            reasons.append(
                f"measured success_rate={definition.evidence.success_rate:.4f} "
                f"over {definition.evidence.samples} samples"
            )
        else:
            reasons.append("cold start: no measured evidence yet")
        matches.append((score, measured, definition, reasons))

    ordered = sorted(matches, key=lambda m: (-m[0], m[2].skill_id, -m[2].version))[
        : query.max_candidates
    ]
    return SkillRetrieval(
        capability=query.capability,
        matches=[
            SkillMatch(
                skill_id=definition.skill_id,
                version=definition.version,
                capability=definition.capability,
                score=score,
                measured=measured,
                reasons=reasons[:MAX_SKILL_MATCH_REASONS],
            )
            for score, measured, definition, reasons in ordered
        ],
        excluded=excluded[:32],
    )
