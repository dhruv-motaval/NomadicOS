"""Formal capability registry (rebuild plan STEP 3).

Every action the runtime can execute resolves to ONE registered capability
ID before it may reach the Security Gate. Capability = system-side contract:
risk class, required approval level, resource kind, network/sandbox flags.
The registry is code + owner policy — never model, memory, planner text,
tool output, or retry state (those are DATA for reasoning, not authority).

Only capabilities the runtime can ACTUALLY enforce today are listed; e.g.
there is no separate process-kill enforcement, so it has no entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.errors import PermissionDenied

RESOURCE_PATH = "path"
RESOURCE_COMMAND = "command"
RESOURCE_URL = "url"
RESOURCE_SCRIPT = "script"


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    tool: str = ""  # registered tool name this capability resolves from
    tool_prefix: str = ""  # or a name prefix (generated tools: 'script.')
    operation: str = ""
    risk: RiskLevel = RiskLevel.MEDIUM
    requires_user_authorization: bool = False
    network_required: bool = False
    sandbox_required: bool = False
    audit_required: bool = True
    resource_kind: str = "none"


def _filesystem_caps() -> list[Capability]:
    defs = [
        ("read", RiskLevel.LOW, RESOURCE_PATH),
        ("list", RiskLevel.LOW, RESOURCE_PATH),
        ("write", RiskLevel.MEDIUM, RESOURCE_PATH),
        # destructive: explicit owner authorization required regardless of
        # any default rule (BP §85, §90 CRITICAL posture)
        ("delete", RiskLevel.CRITICAL, RESOURCE_PATH),
    ]
    return [
        Capability(
            id=f"filesystem.{action}",
            tool="filesystem",
            operation=action,
            risk=risk,
            requires_user_authorization=(action == "delete"),
            sandbox_required=True,
            resource_kind=resource,
        )
        for action, risk, resource in defs
    ]


def _registry() -> dict[str, Capability]:
    caps: list[Capability] = [
        *_filesystem_caps(),
        Capability(
            id="terminal.execute",
            tool="terminal",
            operation="execute",
            risk=RiskLevel.HIGH,
            sandbox_required=True,  # workspace-bound cwd + argv-only (no shell)
            resource_kind=RESOURCE_COMMAND,
        ),
        Capability(
            id="network.fetch",
            tool="web.fetch",
            operation="fetch",
            risk=RiskLevel.MEDIUM,
            network_required=True,  # public GET only; NetworkGateway enforces
            resource_kind=RESOURCE_URL,
        ),
        Capability(
            id="generated_tool.execute",
            tool_prefix="script.",
            operation="execute",
            risk=RiskLevel.HIGH,
            # a generated script is NEVER trusted for being generated, or for
            # having worked before: it needs a fresh owner grant each use
            # (owner grants are one-shot consumed after success, BP §355).
            requires_user_authorization=True,
            sandbox_required=True,  # runs only from data/scripts by contract
            resource_kind=RESOURCE_SCRIPT,
        ),
    ]
    return {c.id: c for c in caps}


_CAPABILITIES = _registry()
# tool/prefix -> (fallback capability id when no action arg, {action: id})
_BY_TOOL: dict[str, Capability] = {}
_BY_PREFIX: dict[str, Capability] = {}
_FILESYSTEM_ACTIONS: dict[str, Capability] = {}

for _cap in _CAPABILITIES.values():
    if _cap.tool == "filesystem":
        _FILESYSTEM_ACTIONS[_cap.operation] = _cap
    if _cap.tool_prefix:
        _BY_PREFIX[_cap.tool_prefix] = _cap
    elif _cap.tool:
        _BY_TOOL[_cap.tool] = _cap


def resolve(tool_name: str, arguments: dict | None = None) -> Capability:
    """Deterministic system resolution — the ONLY way a capability is born.

    Unknown tool / unknown filesystem action / capability not registered:
    PermissionDenied (fail closed, BP §85) BEFORE policy or execution."""
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool_name == "filesystem":
        action = arguments.get("action", "read")
        cap = _FILESYSTEM_ACTIONS.get(str(action))
        if cap is None:
            raise PermissionDenied(
                f"CAPABILITY_NOT_REGISTERED: filesystem.{action}",
                context={"reason_code": "CAPABILITY_NOT_REGISTERED"},
            )
        return cap
    for prefix, cap in _BY_PREFIX.items():
        if tool_name.startswith(prefix):
            return cap
    cap = _BY_TOOL.get(tool_name)
    if cap is None:
        raise PermissionDenied(
            f"CAPABILITY_NOT_REGISTERED: {tool_name}",
            context={"reason_code": "CAPABILITY_NOT_REGISTERED"},
        )
    return cap


def registered_ids() -> list[str]:
    return sorted(_CAPABILITIES)


_MANAGED_TOOLS = frozenset({"filesystem", "terminal", "web.fetch"})
_MANAGED_PREFIXES = ("script.",)

# ToolRisk (declared by the tool author) -> RiskLevel used by policy/audit.
_SPEC_RISK = {
    "read_only": RiskLevel.LOW,
    "state_changing": RiskLevel.MEDIUM,
    "destructive": RiskLevel.CRITICAL,
    "network": RiskLevel.MEDIUM,
    "administrative": RiskLevel.HIGH,
    "security_critical": RiskLevel.CRITICAL,
}


def risk_from_spec(spec_risk_value: str) -> RiskLevel:
    return _SPEC_RISK[spec_risk_value]


def is_managed(tool_name: str) -> bool:
    """True when the registry owns the capability contract for this tool
    (explicit actions are enumerated -> unknown actions STAY denied)."""
    return tool_name in _MANAGED_TOOLS or any(tool_name.startswith(p) for p in _MANAGED_PREFIXES)


def default_capability(tool_name: str, spec_risk: RiskLevel) -> Capability:
    """Owner-registered tool without an explicit capability entry gets a
    deterministic generic contract: policy still keys on the tool name and
    the declared spec risk; absence of any rule remains a fail-closed DENY."""
    return Capability(
        id=f"{tool_name}.execute",
        tool=tool_name,
        operation="execute",
        risk=spec_risk,
        resource_kind="none",
    )


__all__ = [
    "Capability",
    "default_capability",
    "is_managed",
    "registered_ids",
    "resolve",
    "risk_from_spec",
    "RESOURCE_COMMAND",
    "RESOURCE_PATH",
    "RESOURCE_SCRIPT",
    "RESOURCE_URL",
]
