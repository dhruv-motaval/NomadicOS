"""Sprint 9 — brain security seam: capability request → availability →
selection → bounded plan, behind a protocol; no concrete security tool,
OS, subprocess, or network imports; fail-closed parsing (mirrors the
StructuredGenerator / MemoryRetriever / RoleModelRouter / SkillCatalog
seams)."""

from __future__ import annotations

import ast
import pathlib

import pytest

from nomadicos.brain.errors import ModelOutputRejected
from nomadicos.brain.security import (
    SecurityCapabilityRequest,
    SecurityCatalog,
    SecurityOption,
    SecurityResearchPlan,
    parse_security_options,
    plan_security_research,
    select_security_option,
)


def request(**overrides) -> SecurityCapabilityRequest:
    base = dict(engagement_id="eng_lab", target_id="tgt_repo")
    base.update(overrides)
    return SecurityCapabilityRequest(**base)


def option(**overrides) -> dict:
    base = dict(
        capability="RECON",
        engagement_id="eng_lab",
        target_id="tgt_repo",
        sandbox_id="sb_lab",
        risk="LOW",
        available=True,
        reasons=["granted by engagement eng_lab"],
    )
    base.update(overrides)
    return base


class ScriptedCatalog:
    """Deterministic SecurityCatalog fake (protocol only, no brick import)."""

    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.seen: list[SecurityCapabilityRequest] = []

    def security_capabilities(self, request: SecurityCapabilityRequest) -> list[dict]:
        self.seen.append(request)
        return self.hits


def test_request_is_bounded_closed_and_fail_closed() -> None:
    assert isinstance(ScriptedCatalog([]), SecurityCatalog)  # structural protocol
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        request(engagement_id="bad id!")  # invalid engagement id
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        request(capability="CREDENTIAL_THEFT")  # unknown taxonomy kind
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SecurityCapabilityRequest.model_validate(
            {**request().model_dump(mode="json"), "authorized": True}
        )


def test_option_parsing_fails_closed() -> None:
    options = parse_security_options([option()], request())
    assert options[0].capability == "RECON"
    # binding mismatch rejected (no target/engagement substitution)
    with pytest.raises(ModelOutputRejected):
        parse_security_options([option(target_id="tgt_other")], request())
    # unknown capability rejected
    with pytest.raises(ModelOutputRejected):
        parse_security_options([option(capability="EXPLOIT")], request())
    # authority-shaped extras rejected
    with pytest.raises(ModelOutputRejected):
        parse_security_options([{**option(), "bypass": True}], request())
    # malformed entry rejected
    with pytest.raises(ModelOutputRejected):
        parse_security_options([{"capability": "RECON", "score": "high"}], request())


def test_deterministic_lowest_risk_selection() -> None:
    options = parse_security_options(
        [option(capability="VULNERABILITY_VALIDATION", risk="HIGH"), option()], request()
    )
    selected = select_security_option(options)
    assert selected is not None and selected.capability == "RECON"  # lowest risk wins
    assert (
        select_security_option([SecurityOption.model_validate(option(available=False))]) is None
    )  # fail safe
    assert select_security_option([]) is None
    # deterministic across runs
    again = parse_security_options(
        [option(capability="VULNERABILITY_VALIDATION", risk="HIGH"), option()], request()
    )
    assert select_security_option(again) == selected


def test_plan_is_bounded_and_authority_free() -> None:
    selected = select_security_option(parse_security_options([option()], request()))
    plan = plan_security_research(selected, "sb_lab", {"path": "src"}, rationale="recon in scope")
    assert plan.expected_evidence == "OBSERVATION"  # from the closed taxonomy
    assert plan.capability == "RECON" and plan.sandbox_id == "sb_lab"
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        SecurityResearchPlan.model_validate({**plan.model_dump(mode="json"), "executed": True})
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        plan_security_research(selected, "sb_lab", {f"k{i}": 1 for i in range(9)})


def test_brain_security_module_imports_nothing_dangerous() -> None:
    """AST check: no concrete security tools, OS/subprocess/network APIs,
    or runtime bricks are imported by the brain seam."""
    path = (
        pathlib.Path(__file__).resolve().parents[2] / "src" / "nomadicos" / "brain" / "security.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            for name in [a.name for a in node.names] + [module]:
                assert "subprocess" not in name
                assert "socket" not in name
                assert "http" not in name
                assert "nomadicos.tools" not in name
                assert "nomadicos.executor" not in name
                assert "nomadicos.authority" not in name
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            assert name not in ("execute", "system", "popen"), name


def test_scripted_catalog_end_to_end_discovery_selection_plan() -> None:
    catalog = ScriptedCatalog([option()])
    req = request()
    options = parse_security_options(catalog.security_capabilities(req), req)
    selected = select_security_option(options)
    plan = plan_security_research(selected, "sb_lab", {"path": "src"}, rationale="recon in scope")
    assert plan.engagement_id == "eng_lab"
    assert len(catalog.seen) == 1  # exactly one bounded request
