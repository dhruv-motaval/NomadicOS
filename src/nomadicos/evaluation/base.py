"""Verification contracts (BP §28, §144-146, §366).

A VerificationCheck inspects *evidence*, not claims. Verifiers run ordered
checks and produce a verdict; any failed check fails the verification.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Evidence:
    """Observed facts about an action's outcome (BP §146)."""

    kind: str  # e.g. "filesystem", "terminal", "screen", "browser"
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class EvaluationVerdict:
    verified: bool
    checks: tuple[VerificationCheck, ...]

    @property
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.passed)
        return f"{passed}/{len(self.checks)} checks passed"


CheckFn = Any  # callable(Evidence) -> VerificationCheck


class Verifier(ABC):
    """Runs evidence checks for one action kind (BP §144 verification adapters)."""

    @abstractmethod
    def checks(self, evidence: "Evidence | None" = None) -> list: ...

    async def verify(self, evidence: Evidence) -> EvaluationVerdict:
        results = []
        for check in self.checks(evidence):
            results.append(check(evidence))
        return EvaluationVerdict(
            verified=all(c.passed for c in results) if results else False,
            checks=tuple(results),
        )


def file_exists_check(path_field: str = "path", exists_field: str = "exists") -> CheckFn:
    def _check(evidence: Evidence) -> VerificationCheck:
        passed = bool(evidence.facts.get(exists_field))
        return VerificationCheck(
            name=f"file_exists:{evidence.facts.get(path_field, '?')}",
            passed=passed,
            detail=f"{exists_field}={evidence.facts.get(exists_field)}",
        )

    return _check


def hash_changed_check(
    before_field: str = "hash_before", after_field: str = "hash_after"
) -> CheckFn:
    def _check(evidence: Evidence) -> VerificationCheck:
        before = evidence.facts.get(before_field)
        after = evidence.facts.get(after_field)
        # Creation (no hash_before) counts as a change when the file now exists.
        passed = (before is None and bool(after)) or (
            bool(before) and bool(after) and before != after
        )
        return VerificationCheck(name="hash_changed", passed=passed, detail=f"{before} -> {after}")

    return _check


def exit_code_check(field_name: str = "exit_code", expected: int = 0) -> CheckFn:
    def _check(evidence: Evidence) -> VerificationCheck:
        code = evidence.facts.get(field_name)
        passed = isinstance(code, int) and code == expected
        return VerificationCheck(name=f"exit_code=={expected}", passed=passed, detail=f"got {code}")

    return _check


def contains_check(field_name: str, needle: str) -> CheckFn:
    def _check(evidence: Evidence) -> VerificationCheck:
        haystack = str(evidence.facts.get(field_name, ""))
        return VerificationCheck(
            name=f"contains:{needle[:32]}",
            passed=needle in haystack,
            detail=f"searched {field_name}",
        )

    return _check


def output_not_empty_check(field_name: str = "output") -> CheckFn:
    def _check(evidence: Evidence) -> VerificationCheck:
        value = evidence.facts.get(field_name)
        passed = bool(value)
        detail = f"got {type(value).__name__}"
        return VerificationCheck(name="output_not_empty", passed=passed, detail=detail)

    return _check


__all__ = [
    "EvaluationVerdict",
    "Evidence",
    "Verifier",
    "VerificationCheck",
]
