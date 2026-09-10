"""Constitution & Control (BP §4, §76): immutable invariants in code +
schema-validated versioned YAML policies. Fail closed (ADR-0013)."""

from nomadicos.constitution.invariants import (
    INVARIANT_IDS,
    Invariant,
    validate_policy_against_invariants,
)
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.constitution.policy_schema import OwnerPolicy, PolicyDocument

ImmutableInvariantsPolicy = PolicyDocument  # single policy family in v0.1

__all__ = [
    "INVARIANT_IDS",
    "ImmutableInvariantsPolicy",
    "Invariant",
    "OwnerPolicy",
    "PolicyDocument",
    "PolicyEngine",
    "validate_policy_against_invariants",
]
