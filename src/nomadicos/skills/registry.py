"""Skill registry (Sprint 8): deterministic registration/lookup/enumeration.

Reuse rule: this registry indexes declarative skill KNOWLEDGE only. It
does NOT replace the tool registry (execution dispatch), the model
registry (Sprint 7), or any persistence system — a skill is data that
POINTS at an existing capability ("tool.operation"); execution always
travels the existing Action IR → validator → authorization → executor
path.

Deterministic: enumeration is (skill_id, version)-sorted; capability
index rebuilt on every registration. Bounded: at most MAX_SKILLS total
and MAX_SKILL_VERSIONS per skill (oldest evicted deterministically).
Fail-safe: malformed definitions cannot enter (contracts validate);
duplicates are rejected explicitly, never silently overwritten.
"""

from __future__ import annotations

from nomadicos.contracts.skills import SkillDefinition
from nomadicos.kernel.errors import InvalidProposal

MAX_SKILLS = 256
MAX_SKILL_VERSIONS = 8


class SkillRegistry:
    def __init__(self, *, max_skills: int = MAX_SKILLS) -> None:
        if max_skills < 1:
            raise ValueError("max_skills must be >= 1")
        self._max = max_skills
        #: (skill_id, version) -> definition (never overwritten)
        self._skills: dict[tuple[str, int], SkillDefinition] = {}
        #: capability -> sorted skill ids (rebuilt on registration)
        self._by_capability: dict[str, list[str]] = {}

    # ------------------------------------------------------------- write ---
    def register(self, definition: SkillDefinition) -> SkillDefinition:
        """Register one skill version. Duplicates (same id+version) are
        rejected explicitly — silent overwrite is never allowed."""
        key = (definition.skill_id, definition.version)
        if key in self._skills:
            raise InvalidProposal(
                f"duplicate skill {definition.skill_id!r} version {definition.version}"
            )
        if len(self._skills) >= self._max:
            raise InvalidProposal(f"skill registry full ({self._max})")
        self._skills[key] = definition
        self._reindex()
        return definition

    def set_health(self, skill_id: str, *, version: int | None = None, healthy: bool) -> bool:
        """Deterministic health transition; unknown skill fails closed."""
        definition = self.lookup(skill_id, version)
        if definition is None:
            return False
        self._skills[(definition.skill_id, definition.version)] = definition.model_copy(
            update={"healthy": healthy}
        )
        return True

    def set_enabled(self, skill_id: str, *, version: int | None = None, enabled: bool) -> bool:
        definition = self.lookup(skill_id, version)
        if definition is None:
            return False
        self._skills[(definition.skill_id, definition.version)] = definition.model_copy(
            update={"enabled": enabled}
        )
        return True

    # -------------------------------------------------------------- read ---
    def lookup(self, skill_id: str, version: int | None = None) -> SkillDefinition | None:
        """Latest version when none given (deterministic max)."""
        if version is not None:
            return self._skills.get((skill_id, version))
        versions = sorted(v for (sid, v) in self._skills if sid == skill_id)
        return self._skills[(skill_id, versions[-1])] if versions else None

    def by_capability(self, capability: str) -> list[SkillDefinition]:
        ids = self._by_capability.get(capability, [])
        out: list[SkillDefinition] = []
        for skill_id in ids:
            definition = self.lookup(skill_id)
            if definition is not None:
                out.append(definition)
        return out

    def enumerate_skills(self) -> list[SkillDefinition]:
        """Deterministic (skill_id, version) order."""
        return [
            self._skills[key] for key in sorted(self._skills, key=lambda k: (k[0], k[1]))
        ]

    def capabilities(self) -> list[str]:
        return sorted(self._by_capability)

    def __len__(self) -> int:
        return len(self._skills)

    # ------------------------------------------------------------ intern ---
    def _reindex(self) -> None:
        index: dict[str, list[str]] = {}
        for (skill_id, _version), definition in self._skills.items():
            index.setdefault(definition.capability, []).append(skill_id)
        self._by_capability = {
            capability: sorted(set(ids)) for capability, ids in index.items()
        }
        # deterministic version eviction: keep newest MAX_SKILL_VERSIONS per id
        per_id: dict[str, list[int]] = {}
        for (skill_id, version) in self._skills:
            per_id.setdefault(skill_id, []).append(version)
        for skill_id, versions in per_id.items():
            if len(versions) > MAX_SKILL_VERSIONS:
                for version in sorted(versions)[: len(versions) - MAX_SKILL_VERSIONS]:
                    del self._skills[(skill_id, version)]
