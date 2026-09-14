import pytest

from nomadicos.tools.base import ToolContext, ToolResult, ToolRisk, validate_against_schema
from nomadicos.tools.fake import FakeTool


def test_fake_tool_records_execution() -> None:
    tool = FakeTool(name="fake.echo", result=ToolResult(success=True, data={"v": 42}))
    context = ToolContext(user_id="user-1", task_id="t-1")

    import asyncio

    arguments = asyncio.run(tool.validate_arguments({}))
    result = asyncio.run(tool.execute(arguments, context))

    assert result.success is True
    assert result.data == {"v": 42}
    assert tool.executed == [{}]
    assert tool.spec.risk is ToolRisk.READ_ONLY


def test_schema_validation_rejects_unknown_and_missing() -> None:
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 10},
            "count": {"type": "integer"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    validate_against_schema({"path": "a.txt", "count": 3}, schema)
    with pytest.raises(ValueError, match="missing required"):
        validate_against_schema({"count": 1}, schema)
    with pytest.raises(ValueError, match="unknown argument"):
        validate_against_schema({"path": "a", "extra": True}, schema)
    with pytest.raises(ValueError, match="must be integer"):
        validate_against_schema({"path": "a", "count": "3"}, schema)
    with pytest.raises(ValueError, match="too long"):
        validate_against_schema({"path": "x" * 11}, schema)


def test_risk_levels_cover_blueprint_90() -> None:
    assert {r.value for r in ToolRisk} == {
        "read_only",
        "state_changing",
        "destructive",
        "network",
        "administrative",
        "security_critical",
    }


def test_dry_run_default_unsupported() -> None:
    tool = FakeTool()  # supports_dry_run=False
    import asyncio

    result = asyncio.run(tool.dry_run({}, ToolContext(user_id="u")))
    assert result.success is False


def test_builtin_start_routes_through_cmd() -> None:
    from nomadicos.tools.terminal import build_command

    argv = build_command({"command": "start chrome"})
    assert argv == ["cmd", "/c", "start", "chrome"]


def test_builtin_metacharacters_rejected() -> None:
    import pytest

    from nomadicos.core.errors import ValidationError
    from nomadicos.tools.terminal import build_command

    with pytest.raises(ValidationError):
        build_command({"command": "start chrome & calc"})


def test_multiword_exe_command_is_tokenized() -> None:
    from nomadicos.tools.terminal import build_command

    argv = build_command({"command": "git commit", "args": ["-m", "hi"]})
    assert argv == ["git", "commit", "-m", "hi"]
