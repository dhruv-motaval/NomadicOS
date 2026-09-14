"""Tool contract: strict schemas, trust levels, evidence-bearing results.

- BP §86: never execute raw model text — structured, schema-validated calls only.
- BP §90: every tool carries a trust/risk classification.
- BP §139: dry-run support for high-risk operations.
- BP §141: descriptions state purpose/arguments/risk/side effects.
- BP §143: results return success/data/error/**evidence**.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ToolName = str


class ToolRisk(StrEnum):
    """Tool trust classification (BP §90)."""

    READ_ONLY = "read_only"
    STATE_CHANGING = "state_changing"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"
    ADMINISTRATIVE = "administrative"
    SECURITY_CRITICAL = "security_critical"


class ToolSpec(BaseModel):
    """Self-describing tool metadata (BP §141)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{2,63}$")]
    description: str = Field(min_length=1, max_length=2048)
    risk: ToolRisk
    arguments_schema: dict[str, Any]  # JSON-schema style; validated at the gateway
    evidence_kind: str = "terminal"  # BP §144 verifier kind for this tool's results
    side_effects: list[str] = Field(default_factory=list)
    supports_dry_run: bool = False


class ToolResult(BaseModel):
    """Evidence-bearing result envelope (BP §143)."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    # identity correlation (stamped at the gateway/executor boundary)
    task_id: str | None = None
    step_id: str | None = None
    attempt: int | None = None

    @classmethod
    def failure(
        cls,
        error: str,
        evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> "ToolResult":
        return cls(success=False, error=error, evidence=evidence or {}, error_code=error_code)


class ToolContext(BaseModel):
    """Who is calling, and for what task (BP §399 correlation)."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    session_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    workspace_root: str | None = None


class Tool(ABC):
    """Base for every tool. Execution is always mediated by the Tool Gateway
    (BP §11-12): tools never self-authorize."""

    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    async def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize arguments against the spec schema. Invalid ⇒ raise
        (BP §142: reject, never guess)."""

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult: ...

    async def dry_run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Default: unsupported (BP §139 — tools opt in via supports_dry_run)."""
        return ToolResult.failure(
            f"tool {self.spec.name} does not support dry-run",
            evidence={"supports_dry_run": self.spec.supports_dry_run},
        )


def validate_against_schema(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Minimal JSON-schema validation (type/required/enum/minLength/maxLength).

    Reject unknown properties when the schema says so (BP §142: reject, never
    guess). Full JSON-schema comes with Phase 4; the subset keeps the contract
    testable from Phase 0.
    """
    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}))
        unknown = set(arguments) - allowed
        if unknown:
            raise ValueError(f"unknown argument(s): {sorted(unknown)}")
    for name, constraint in schema.get("properties", {}).items():
        if name not in arguments:
            if name in schema.get("required", []):
                raise ValueError(f"missing required argument: {name}")
            continue
        value = arguments[name]
        expected = constraint.get("type")
        checks = {
            "string": lambda v: isinstance(v, str),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
        }
        if expected in checks and not checks[expected](value):
            raise ValueError(f"argument {name} must be {expected}")
        if isinstance(value, str):
            if "minLength" in constraint and len(value) < constraint["minLength"]:
                raise ValueError(f"argument {name} too short")
            if "maxLength" in constraint and len(value) > constraint["maxLength"]:
                raise ValueError(f"argument {name} too long")
        if "enum" in constraint and value not in constraint["enum"]:
            raise ValueError(f"argument {name} must be one of {constraint['enum']}")


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolRisk", "ToolSpec", "validate_against_schema"]
