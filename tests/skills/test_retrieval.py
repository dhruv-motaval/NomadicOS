"""Sprint 8 — skill retrieval: capability matching, deterministic ranking
and tie-breaking, prerequisite/input/risk/role filters, bounded output."""

from __future__ import annotations

import pytest

from nomadicos.contracts.skills import (
    SkillDefinition,
    SkillEvidence,
    SkillParameter,
    SkillQuery,
    SkillRisk,
)
from nomadicos.skills.registry import SkillRegistry
from nomadicos.skills.retrieval import rank_skills


def skill(
    skill_id: str,
    *,
    capability: str = "terminal.execute",
    version: int = 1,
    enabled: bool = True,
    healthy: bool = True,
    roles: list[str] | None = None,
    prereqs: list[str] | None = None,
    inputs: list[SkillParameter] | None = None,
    risk: SkillRisk = SkillRisk.LOW,
    evidence: SkillEvidence | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        version=version,
        name=skill_id.replace("-", " "),
        description=f"skill {skill_id}",
        capability=capability,
        inputs=inputs if inputs is not None else [SkillParameter(name="command", type="string")],
        prerequisites=prereqs or [],
        supported_roles=roles or [],
        risk=risk,
        enabled=enabled,
        healthy=healthy,
        evidence=evidence or SkillEvidence(),
    )


def reg(*definitions: SkillDefinition) -> SkillRegistry:
    registry = SkillRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry


def test_capability_match_required() -> None:
    result = rank_skills(
        reg(skill("a")), SkillQuery(capability="filesystem.write", provided_args=["command"])
    )
    assert result.matches == []  # capability mismatch: no silent substitution


def test_measured_outranks_cold_start() -> None:
    measured = skill(
        "measured", evidence=SkillEvidence(samples=4, successes=4, failures=0,
                                            success_rate=1.0)
    )
    cold = skill("cold")
    query = SkillQuery(capability="terminal.execute", provided_args=["command"])
    result = rank_skills(reg(cold, measured), query)
    assert [m.skill_id for m in result.matches] == ["measured", "cold"]
    assert result.matches[0].measured is True
    assert result.matches[0].score > result.matches[1].score
    assert "cold start" in result.matches[1].reasons[1]


def test_higher_success_rate_ranks_higher() -> None:
    good = skill(
        "good", evidence=SkillEvidence(samples=5, successes=5, failures=0,
                                        success_rate=1.0)
    )
    mediocre = skill(
        "mediocre", evidence=SkillEvidence(samples=5, successes=3, failures=2,
                                            success_rate=0.6)
    )
    query = SkillQuery(capability="terminal.execute", provided_args=["command"])
    result = rank_skills(reg(mediocre, good), query)
    assert [m.skill_id for m in result.matches] == ["good", "mediocre"]


def test_disabled_and_unhealthy_excluded_auditably() -> None:
    result = rank_skills(
        reg(skill("off", enabled=False), skill("sick", healthy=False), skill("ok")),
        SkillQuery(capability="terminal.execute", provided_args=["command"]),
    )
    assert [m.skill_id for m in result.matches] == ["ok"]
    assert any("disabled" in e for e in result.excluded)
    assert any("unhealthy" in e for e in result.excluded)


def test_role_compatibility_filter() -> None:
    result = rank_skills(
        reg(skill("restricted", roles=["EVALUATOR"])),
        SkillQuery(capability="terminal.execute", role="ACTOR", provided_args=["command"]),
    )
    assert result.matches == []
    assert any("role" in e for e in result.excluded)
    ok = rank_skills(
        reg(skill("restricted", roles=["ACTOR"])),
        SkillQuery(capability="terminal.execute", role="ACTOR", provided_args=["command"]),
    )
    assert [m.skill_id for m in ok.matches] == ["restricted"]


def test_prerequisite_filter() -> None:
    result = rank_skills(
        reg(skill("gated", prereqs=["filesystem.exists"])),
        SkillQuery(capability="terminal.execute", provided_args=["command"]),
    )
    assert result.matches == []  # unmet prerequisites fail closed
    assert any("prerequisites" in e for e in result.excluded)
    met = rank_skills(
        reg(skill("gated", prereqs=["filesystem.exists"])),
        SkillQuery(capability="terminal.execute", satisfied_prerequisites=["filesystem.exists"],
                   provided_args=["command"]),
    )
    assert [m.skill_id for m in met.matches] == ["gated"]


def test_input_compatibility_filter() -> None:
    strict = skill("strict", inputs=[SkillParameter(name="command", type="string", required=True),
                                      SkillParameter(name="cwd", type="string", required=True)])
    result = rank_skills(
        reg(strict), SkillQuery(capability="terminal.execute", provided_args=["command"])
    )
    assert result.matches == []  # required input not provided
    ok = rank_skills(
        reg(strict),
        SkillQuery(capability="terminal.execute", provided_args=["command", "cwd"]),
    )
    assert [m.skill_id for m in ok.matches] == ["strict"]


def test_risk_ceiling_filter() -> None:
    result = rank_skills(
        reg(skill("risky", risk=SkillRisk.HIGH)),
        SkillQuery(capability="terminal.execute", max_risk=SkillRisk.LOW),
    )
    assert result.matches == []
    assert any("risk" in e for e in result.excluded)


def test_deterministic_order_and_tiebreak() -> None:
    a = skill("a-skill")
    b = skill("b-skill")
    query = SkillQuery(capability="terminal.execute", provided_args=["command"])
    result = rank_skills(reg(b, a), query)
    assert [m.skill_id for m in result.matches] == ["a-skill", "b-skill"]  # tie → id order
    # deterministic version handling: only the LATEST version of a skill
    # competes (old versions never shadow the active one)
    registry = reg(skill("alpha", version=1), skill("alpha", version=2))
    by_version = rank_skills(registry, query)
    assert [m.version for m in by_version.matches] == [2]


def test_retrieval_is_bounded_and_deterministic() -> None:
    registry = reg(*[skill(f"s{i:02d}") for i in range(20)])
    query = SkillQuery(capability="terminal.execute", max_candidates=3,
                       provided_args=["command"])
    first = rank_skills(registry, query)
    second = rank_skills(registry, query)
    assert first == second  # deterministic
    assert len(first.matches) == 3  # bounded
    assert len(first.excluded) <= 32
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillQuery(capability="terminal.execute", max_candidates=64)
