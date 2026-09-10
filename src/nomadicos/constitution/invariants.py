"""Immutable security invariants (BP §4.2) — executable Constitution (ADR-0013).

These hold over all policies, models, agents, and learned behavior (BP §190,
§367). `validate_policy_against_invariants` is the structural check applied to
every policy document at load time; the runtime test suite (BP §191) verifies
the *system*, this module verifies the *policy artifacts*.
"""

from typing import Any

from nomadicos.core.errors import SecurityPolicyViolation

InvariantID = str


class Invariant:
    """Declarative invariant record — the fifteen canonical rules (§4.2)."""

    def __init__(self, id: str, title: str, blueprint_ref: str) -> None:
        self.id = id
        self.title = title
        self.blueprint_ref = blueprint_ref


INVARIANTS: tuple[Invariant, ...] = (
    Invariant("I1", "Local-only inference — no external LLM calls", "BP §1.3, §200, §286"),
    Invariant("I2", "No OmniRouter / Hermes / separate Skill System", "BP §1.1, §370"),
    Invariant("I3", "No privilege escalation by the model", "BP §42"),
    Invariant("I4", "No policy modification by the model", "BP §42, §260"),
    Invariant("I5", "No gate bypass — Tool Gateway + Security Gate always", "BP §11-13, §73"),
    Invariant("I6", "Fail closed on unknown/invalid security state", "BP §85"),
    Invariant("I7", "No audit tampering; audit never model-controlled", "BP §41-42"),
    Invariant("I8", "No evidence fabrication; report verified outcomes only", "BP §69, §366"),
    Invariant("I9", "No unauthorized persistence or self-preservation", "BP §68"),
    Invariant("I10", "Bounded execution; budgets enforced outside the model", "BP §52, §70, §72"),
    Invariant("I11", "Data locality — private data never leaves the machine", "BP §199-200"),
    Invariant("I12", "Secrets containment — no plaintext secrets in outputs", "BP §256"),
    Invariant("I13", "Supply-chain validation for models and extensions", "BP §150-152"),
    Invariant("I14", "Learned policy never overrides immutable policy", "BP §190"),
    Invariant("I15", "User authority above learned behavior, invariants absolute", "BP §367"),
)

INVARIANT_IDS: frozenset[str] = frozenset(inv.id for inv in INVARIANTS)

# Policy vocabulary that would express a privileged capability. Owner policies
# operate at a lower precedence tier than the invariants (BP §262), so these can
# never appear as *grants* in any policy document.
_FORBIDDEN_GRANT_KEYS = frozenset(
    {
        "allow_policy_modification",
        "allow_config_modification",
        "allow_privilege_escalation",
        "allow_audit_modification",
        "allow_external_model_inference",
        "allow_private_data_export",
        "bypass_security_gate",
        "bypass_tool_gateway",
        "disable_invariants",
        "disable_audit",
        "unbounded_execution",
    }
)


def validate_policy_against_invariants(policy: dict[str, Any]) -> None:
    """Structural Constitution check over a raw policy mapping (ADR-0013).

    Raises SecurityPolicyViolation when the policy attempts to grant any
    invariant-protected capability. Fail closed (BP §85).
    """

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).strip().lower()
                if normalized in _FORBIDDEN_GRANT_KEYS and value not in (False, None):
                    raise SecurityPolicyViolation(
                        f"policy grants invariant-protected capability: {key}",
                        context={"invariant": "I3/I4/I5/I11"},
                    )
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(policy)


__all__ = [
    "INVARIANT_IDS",
    "INVARIANTS",
    "Invariant",
    "validate_policy_against_invariants",
]
