"""Deterministic verification adapters (BP §144: per-tool-kind checks)."""

from nomadicos.evaluation.base import Verifier


class FilesystemVerifier(Verifier):
    """Verifies filesystem operations per action (BP §144, §146).

    write → existence + hash change/creation; read/list/delete → existence
    signals from the tool's own evidence.
    """

    def __init__(self, *, require_hash_change: bool = True) -> None:
        self._require_hash_change = require_hash_change

    def checks(self, evidence=None) -> list:
        from nomadicos.evaluation.base import (
            file_exists_check,
            hash_changed_check,
            output_not_empty_check,
        )

        action = (evidence.facts.get("action") if evidence else None) or "write"
        if action == "write":
            checks = [file_exists_check()]
            if self._require_hash_change:
                checks.append(hash_changed_check())
            return checks
        if action == "list":
            return [output_not_empty_check("entries")]
        if action == "read":
            return [output_not_empty_check("content"), file_exists_check()]

        # delete: evidence records exists=False after removal
        def deleted_check(ev) -> "object":
            from nomadicos.evaluation.base import VerificationCheck

            passed = ev.facts.get("exists") is False
            return VerificationCheck(
                name="deleted", passed=passed, detail=f"exists={ev.facts.get('exists')}"
            )

        return [deleted_check]


class TerminalVerifier(Verifier):
    """Verifies terminal commands: exit code + non-empty output (BP §144)."""

    def __init__(self, expected_exit_code: int = 0) -> None:
        self._expected = expected_exit_code

    def checks(self, evidence=None) -> list:
        from nomadicos.evaluation.base import exit_code_check, output_not_empty_check

        return [exit_code_check(expected=self._expected), output_not_empty_check("stdout")]


__all__ = ["FilesystemVerifier", "TerminalVerifier"]
