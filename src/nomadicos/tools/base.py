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


def error_text(exc: ValidationError) -> str:
    """Human-readable validation error text: the replanning layer (model)
    must be able to self-correct from it - raw pydantic dicts are opaque."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ())) or "args"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


class Tool(ABC):
    name: ClassVar[str]
    #: operation -> typed argument model
    ops: ClassVar[dict[str, type[BaseModel]]]
    #: What syntax errors should suggest instead. Read-only grounding for
    #: proposal authors: when this tool rejects a proposal as INVALID_PROPOSAL,
    #: the rejection message names the best alternative so the model can
    #: self-correct on the next attempt (never "fixed" into an action, SPEC §19).
    syntax_hint: ClassVar[str] = ""
    #: Which models get the per-request syntax grounding (auto-injection of
    #: this tool's syntax_hint into the proposal prompt). Default is empty:
    #: no model gets it unless the owner opts a model in. The grounding is
    #: per-model because smaller models (e.g. LFM3-class) benefit from
    #: explicit vocabulary guardrails, while stronger models do not need a
    #: larger prompt — and prompt size is a real cost on every call.
    grounding_models: ClassVar[frozenset[str]] = frozenset()

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
            raise InvalidProposal(f"{self.name}.{operation} args: {error_text(exc)}") from exc

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

    def syntax_hint(self, tool: str) -> str:
        impl = self._tools.get(tool)
        return impl.syntax_hint if impl is not None else ""

    def grounding_hint(self, model_id: str) -> str:
        """Aggregated syntax grounding for ONE model (reusable opt-in).

        Each tool declares ``grounding_models``; the model only sees a
        tool's ``syntax_hint`` when it is in that set. Empty for every
        non-opted-in model. Read-only guidance — nothing here is authority.
        """
        hints: list[str] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            if model_id in tool.grounding_models and tool.syntax_hint:
                hints.append(f"- {name}: {tool.syntax_hint}")
        if not hints:
            return ""
        return (
            "WINDOWS/LOCATION VOCABULARY GROUNDING (use these spellings):\n"
            + "\n".join(hints)
            + "\n"
        )

    def capability_args(self) -> dict[str, str]:
        """Every registered "<tool>.<operation>" -> its argument keys, required ones
        marked with ``*`` (data, not authority)."""
        return {
            f"{n}.{op}": ",".join(
                k + ("*" if f.is_required() else "")
                for k, f in self._tools[n].ops[op].model_fields.items()
            )
            for n in sorted(self._tools)
            for op in sorted(self._tools[n].ops)
        }

    def catalog_lines(self) -> list[str]:
        """Human/model-readable listing of what EXISTS (data, not authority)."""
        lines: list[str] = []
        for name in sorted(self._tools):
            tool = self._tools[name]
            for op in sorted(tool.ops):
                fields = ",".join(tool.ops[op].model_fields)
                lines.append(f'- {name}: operation "{op}" args: {{{fields}}}')
        return lines
