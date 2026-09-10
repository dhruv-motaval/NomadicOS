"""Fake tool for testing the gateway/pipeline without side effects."""

from typing import Any

from nomadicos.tools.base import (
    Tool,
    ToolContext,
    ToolResult,
    ToolRisk,
    ToolSpec,
    validate_against_schema,
)


class FakeTool(Tool):
    def __init__(
        self,
        name: str = "fake.echo",
        risk: ToolRisk = ToolRisk.READ_ONLY,
        result: ToolResult | None = None,
        raise_on_execute: Exception | None = None,
        schema: dict[str, Any] | None = None,
        supports_dry_run: bool = False,
    ) -> None:
        self._spec = ToolSpec(
            name=name,
            description=f"fake tool {name}",
            risk=risk,
            arguments_schema=schema or {"type": "object", "properties": {}},
            evidence_kind="terminal",
            supports_dry_run=supports_dry_run,
        )
        self._result = result or ToolResult(success=True, data={"echoed": True})
        self._raise = raise_on_execute
        self.executed: list[dict[str, Any]] = []
        self.dry_runs: list[dict[str, Any]] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validate_against_schema(arguments, self._spec.arguments_schema)
        return dict(arguments)

    async def execute(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        self.executed.append(dict(arguments))
        if self._raise is not None:
            raise self._raise
        return self._result

    async def dry_run(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if not self._spec.supports_dry_run:
            return ToolResult.failure(
                f"tool {self._spec.name} does not support dry-run",
                evidence={"supports_dry_run": False},
            )
        self.dry_runs.append(dict(arguments))
        return ToolResult(success=True, data={"dry_run": True}, evidence={"dry_run": True})


__all__ = ["FakeTool"]
