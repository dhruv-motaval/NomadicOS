"""Tool contract + registry (SPEC §22; registry implements the IR ToolCatalog)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError
from pydantic import Field as PydField

from nomadicos.contracts.action import CapabilityRef
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import Failure, InvalidProposal
from nomadicos.tools.context import ExecutionContext


class ToolOutcome(BaseModel):
    """Honest per-operation result; never a task-level judgment (§27, §6.11)."""

    status: ExecutionStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    evidence: dict[str, Any] = PydField(default_factory=dict)
    process_id: int | None = None
    failure: Failure | None = None
    message: str = ""
    duration_s: float = 0.0


class Tool(ABC):
    name: ClassVar[str]
    #: operation -> typed argument model
    ops: ClassVar[dict[str, type[BaseModel]]]

    def has(self, operation: str) -> bool:
        return operation in self.ops

    def capability(self, operation: str) -> CapabilityRef:
        return CapabilityRef(capability=f"{self.name}.{operation}", resource="")

    def check_args(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        model = self.ops.get(operation)
        if model is None:
            raise InvalidProposal(f"{self.name} has no operation {operation!r}")
        try:
            return model.model_validate(args).model_dump()
        except ValidationError as exc:
            raise InvalidProposal(f"{self.name}.{operation} args: {exc.errors()}") from exc

    @abstractmethod
    async def run(
        self, operation: str, args: dict[str, Any], ctx: ExecutionContext
    ) -> ToolOutcome: ...


class ToolRegistry:
    """Dispatch table. Satisfies the action_ir.ToolCatalog protocol."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, tool: str) -> Tool:
        try:
            return self._tools[tool]
        except KeyError:
            raise InvalidProposal(f"unknown tool {tool!r}") from None

    def find(self, tool: str) -> Tool | None:
        return self._tools.get(tool)

    # ------------------------------------------------- ToolCatalog impl ---
    def has(self, tool: str, operation: str) -> bool:
        impl = self._tools.get(tool)
        return impl is not None and impl.has(operation)

    def capability(self, tool: str, operation: str) -> CapabilityRef:
        impl = self._tools.get(tool)
        if impl is None or not impl.has(operation):
            raise InvalidProposal(f"unknown action {tool}.{operation}")
        return impl.capability(operation)

    def check_args(self, tool: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.get(tool).check_args(operation, args)

    def catalog_lines(self) -> list[str]:
        """Human/model-readable listing of what EXISTS (data, not authority)."""
        lines: list[str] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            for op in sorted(tool.ops):
                fields = ",".join(tool.ops[op].model_fields)
                lines.append(f'- {name}: operation "{op}" args: {{{fields}}}')
        return lines
