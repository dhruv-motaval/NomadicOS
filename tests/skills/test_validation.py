"""Sprint 8 — capability validation: structural fail-closed checks,
strictly separate from authorization."""

from __future__ import annotations

import inspect

from nomadicos.contracts.skills import SkillDefinition, SkillInvocation, SkillParameter
from nomadicos.skills.registry import SkillRegistry
from nomadicos.skills.validation import validate_invocation


def definition(**overrides) -> SkillDefinition:
    base = dict(
        skill_id="write-report",
        name="Write report",
        description="writes a markdown report file",
        capability="filesystem.write",
        inputs=[
            SkillParameter(name="path", type="string", required=True),
            SkillParameter(name="content", type="string", required=True),
            SkillParameter(name="append", type="boolean", required=False),
        ],
    )
    base.update(overrides)
    return SkillDefinition(**base)


def invocation(**overrides) -> SkillInvocation:
    base = dict(
        skill_id="write-report",
        version=1,
        args={"path": "out.md", "content": "# hi", "append": False},
        requested_by="ACTOR",
    )
    base.update(overrides)
    return SkillInvocation(**base)


def reg(*definitions: SkillDefinition) -> SkillRegistry:
    registry = SkillRegistry()
    for item in definitions:
        registry.register(item)
    return registry


def test_valid_invocation_passes_with_audit_trail() -> None:
    result = validate_invocation(reg(definition()), invocation())
    assert result.ok is True
    assert result.reasons == []
    assert result.checked == [
        "exists",
        "enabled",
        "healthy",
        "capability",
        "input_contract",
        "prerequisites",
        "role",
    ]


def test_unknown_skill_fails_closed() -> None:
    result = validate_invocation(reg(), invocation())
    assert result.ok is False
    assert "unknown skill" in result.reasons[0]
    assert result.checked == ["exists"]  # nothing else ran


def test_unknown_version_fails_closed() -> None:
    result = validate_invocation(reg(definition()), invocation(version=9))
    assert result.ok is False and "unknown skill" in result.reasons[0]


def test_disabled_and_unhealthy_fail_closed() -> None:
    result = validate_invocation(reg(definition(enabled=False, healthy=False)), invocation())
    assert result.ok is False
    assert any("disabled" in r for r in result.reasons)
    assert any("unhealthy" in r for r in result.reasons)


def test_input_contract_failures() -> None:
    # missing required arg
    result = validate_invocation(reg(definition()), invocation(args={"path": "out.md"}))
    assert result.ok is False
    assert any("required arg 'content' missing" in r for r in result.reasons)
    # wrong type
    result = validate_invocation(
        reg(definition()), invocation(args={"path": "out.md", "content": "# x", "append": "yes"})
    )
    assert result.ok is False
    assert any("append" in r and "type mismatch" in r for r in result.reasons)
    # unknown arg
    result = validate_invocation(
        reg(definition()),
        invocation(args={"path": "o", "content": "c", "ghost": 1}),
    )
    assert result.ok is False
    assert any("unknown args" in r for r in result.reasons)


def test_missing_prerequisites_fail_closed() -> None:
    gated = definition(prerequisites=["filesystem.exists", "terminal.execute"])
    result = validate_invocation(reg(gated), invocation())
    assert result.ok is False
    assert any("prerequisites missing" in r for r in result.reasons)
    ok = validate_invocation(
        reg(gated),
        invocation(),
        satisfied_prerequisites=["filesystem.exists", "terminal.execute"],
    )
    assert ok.ok is True


def test_role_restriction_fail_closed() -> None:
    restricted = definition(supported_roles=["EVALUATOR"])
    result = validate_invocation(reg(restricted), invocation(requested_by="ACTOR"))
    assert result.ok is False
    assert any("not permitted" in r for r in result.reasons)


def test_validation_is_not_authorization() -> None:
    """Structural separation via AST: the validation module contains no
    authorization imports or calls; it answers structural validity only."""
    import ast
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "nomadicos"
        / "skills"
        / "validation.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            assert "authority" not in module, module
            assert "executor" not in module, module
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            assert name not in ("authorize", "execute"), name
    assert "authorization" in inspect.getdoc(validate_invocation).lower()
