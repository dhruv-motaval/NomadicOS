"""Sprint 8 — skill registry: deterministic registration, lookup,
duplicates, capability indexing, health/enabled transitions, bounds."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nomadicos.contracts.skills import SkillDefinition, SkillParameter
from nomadicos.kernel.errors import InvalidProposal
from nomadicos.skills.registry import MAX_SKILL_VERSIONS, SkillRegistry


def skill(
    skill_id: str,
    *,
    capability: str = "filesystem.write",
    version: int = 1,
    enabled: bool = True,
    healthy: bool = True,
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        version=version,
        name=skill_id.replace("-", " "),
        description=f"skill {skill_id}",
        capability=capability,
        inputs=[SkillParameter(name="path", type="string", required=True)],
        enabled=enabled,
        healthy=healthy,
    )


def test_registration_and_lookup_latest_version() -> None:
    registry = SkillRegistry()
    registry.register(skill("alpha", version=1))
    registry.register(skill("alpha", version=2))
    registry.register(skill("alpha", version=10))
    assert registry.lookup("alpha").version == 10  # latest wins deterministically
    assert registry.lookup("alpha", version=1).version == 1  # exact version
    assert registry.lookup("alpha", version=3) is None
    assert registry.lookup("ghost") is None  # unknown fails closed (None)


def test_duplicate_detection_fails_closed() -> None:
    registry = SkillRegistry()
    registry.register(skill("alpha", version=1))
    with pytest.raises(InvalidProposal):
        registry.register(skill("alpha", version=1))  # same id+version rejected
    assert len(registry) == 1  # not silently overwritten


def test_deterministic_enumeration_and_capability_index() -> None:
    registry = SkillRegistry()
    for sid in ("charlie", "alpha", "bravo"):
        registry.register(skill(sid, capability="terminal.execute"))
    registry.register(skill("delta", capability="filesystem.write"))
    assert [s.skill_id for s in registry.enumerate_skills()] == [
        "alpha",
        "bravo",
        "charlie",
        "delta",
    ]
    assert registry.capabilities() == ["filesystem.write", "terminal.execute"]
    assert [s.skill_id for s in registry.by_capability("terminal.execute")] == [
        "alpha",
        "bravo",
        "charlie",
    ]
    assert registry.by_capability("ghost.op") == []


def test_disabled_and_unhealthy_transitions() -> None:
    registry = SkillRegistry()
    registry.register(skill("alpha"))
    assert registry.set_enabled("alpha", enabled=False) is True
    assert registry.lookup("alpha").enabled is False
    assert registry.set_health("alpha", healthy=False) is True
    assert registry.lookup("alpha").healthy is False
    # unknown skill transitions fail closed (False), never raise
    assert registry.set_enabled("ghost", enabled=False) is False
    assert registry.set_health("ghost", healthy=True) is False


def test_version_bound_evicts_oldest_deterministically() -> None:
    registry = SkillRegistry()
    for version in range(1, MAX_SKILL_VERSIONS + 3):
        registry.register(skill("alpha", version=version))
    versions = sorted(d.version for d in registry.enumerate_skills() if d.skill_id == "alpha")
    assert versions == list(range(3, MAX_SKILL_VERSIONS + 3))  # oldest evicted


def test_registry_bounded() -> None:
    registry = SkillRegistry(max_skills=2)
    registry.register(skill("alpha"))
    registry.register(skill("bravo"))
    with pytest.raises(InvalidProposal):
        registry.register(skill("charlie"))  # hard bound, fail closed


def test_malformed_definitions_cannot_enter() -> None:
    registry = SkillRegistry()
    with pytest.raises(ValidationError):
        registry.register(
            SkillDefinition(
                skill_id="bad capability",  # type: ignore[arg-type]
                name="n",
                description="d",
                capability="tool.op",
            )
        )
    assert len(registry) == 0
