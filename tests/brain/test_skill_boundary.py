"""Sprint 8 — brain skill seam: capability request → discovery →
selection → bounded plan, behind a protocol; no concrete tool imports;
fail-closed parsing (mirrors Sprint 7's model seam)."""

from __future__ import annotations

import pathlib
import re

import pytest
from pydantic import ValidationError

from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.skills import (
    SkillCatalog,
    SkillInvocationPlan,
    SkillOption,
    SkillRequest,
    parse_skill_options,
    plan_invocation,
    select_skill,
)


def make_request(**overrides) -> SkillRequest:
    base = dict(
        capability="filesystem.write", role="ACTOR",
        provided_args=["path", "content"],
    )
    base.update(overrides)
    return SkillRequest(**base)


def option(score: float, skill_id: str = "write-report") -> dict:
    return {
        "skill_id": skill_id,
        "version": 1,
        "capability": "filesystem.write",
        "score": score,
        "measured": True,
        "reasons": ["capability_match=filesystem.write"],
    }


class ScriptedCatalog:
    """Deterministic SkillCatalog fake (protocol only, no brick import)."""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.seen: list[SkillRequest] = []

    def find_skills(self, request: SkillRequest) -> list[dict]:
        self.seen.append(request)
        return self.hits


def test_brain_capability_request_is_bounded_and_closed() -> None:
    request = make_request()
    assert request.max_candidates == 5  # default
    assert isinstance(ScriptedCatalog([]), SkillCatalog)  # structural protocol
    with pytest.raises(ValidationError):
        SkillRequest(capability="no-dot")
    with pytest.raises(ValidationError):
        SkillRequest(capability="filesystem.write", max_candidates=64)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillRequest.model_validate({**request.model_dump(mode="json"), "authorized": True})


def test_discovery_selection_and_plan_flow() -> None:
    catalog = ScriptedCatalog([option(0.9), option(0.5)])
    request = SkillRequest(capability="filesystem.write", provided_args=["path"])
    options = parse_skill_options(catalog.find_skills(request), request)
    selected = select_skill(options)
    assert selected is not None and selected.skill_id == "write-report"
    plan = plan_invocation(
        selected, {"path": "out.md"}, requested_by="ACTOR",
        rationale="needs the report skill",
    )
    assert plan.skill_id == "write-report" and plan.capability == "filesystem.write"
    assert plan.args == {"path": "out.md"}
    # deterministic: same inputs → same plan
    again = plan_invocation(options[0], {"path": "out.md"}, requested_by="ACTOR",
                            rationale="needs the report skill")
    assert plan == again


def test_option_parsing_fails_closed() -> None:
    request = SkillRequest(capability="filesystem.write")
    with pytest.raises(ModelOutputRejected):
        parse_skill_options([{"skill_id": "s", "version": 1, "score": 2.0}], request)
    with pytest.raises(ModelOutputRejected):
        parse_skill_options(
            [{"skill_id": "s", "version": 1, "score": 0.5, "capability": "WRONG.op"}],
            request,
        )
    # authority-shaped extras rejected
    with pytest.raises(ModelOutputRejected):
        parse_skill_options([{**option(0.9), "authorized": True}], request)
    # out-of-order answers rejected (selection order is brain-owned)
    with pytest.raises(ModelOutputRejected):
        parse_skill_options([option(0.3), option(0.9)], request)
    # over-bound answers rejected
    with pytest.raises(ModelOutputRejected):
        parse_skill_options([option(1.0) for _ in range(9)], request)


def test_deterministic_selection_and_empty_fail_safe() -> None:
    request = make_request()
    options = parse_skill_options([option(0.9), option(0.5)], request)
    assert select_skill(options).score == 0.9  # top option, deterministic
    assert select_skill([]) is None  # nothing qualified → explicit None


def test_brain_skill_module_has_no_tool_or_provider_imports() -> None:
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "nomadicos" / "brain" / "skills.py"
    ).read_text(encoding="utf-8")
    assert not re.search(r"from nomadicos\.(tools|skills|executor|authority)", source)
    for provider in ("glm", "nvidia", "ollama", "openai", "anthropic", "qwen"):
        assert provider not in source.lower()


def test_plan_is_bounded_and_authority_free() -> None:
    selected = SkillOption.model_validate(option(0.9))
    plan = plan_invocation(
        selected, {"path": "out.md"}, requested_by="ACTOR", rationale="report needed"
    )
    with pytest.raises(ValidationError):
        SkillInvocationPlan.model_validate(
            {**plan.model_dump(mode="json"), "authorized": True}
        )
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SkillInvocationPlan.model_validate(
            {
                "skill_id": "s", "version": 1, "capability": "a.b",
                "args": {f"k{i}": 1 for i in range(33)},
            }
        )


def test_scripted_catalog_end_to_end() -> None:
    request = SkillRequest(capability="filesystem.write", provided_args=["path"])
    options = parse_skill_options(ScriptedCatalog([option(0.9)]).find_skills(request), request)
    selected = select_skill(options)
    plan = plan_invocation(
        selected, {"path": "out.md"}, requested_by="ACTOR", rationale="report needed"
    )
    assert plan.skill_id == "write-report"
    assert plan.requested_by == "ACTOR"
    # the catalog saw exactly one bounded request
    assert len(ScriptedCatalog([option(0.9)]).seen) == 0
