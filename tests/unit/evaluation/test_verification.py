from nomadicos.evaluation.base import Evidence
from nomadicos.evaluation.deterministic import FilesystemVerifier, TerminalVerifier


async def test_filesystem_verifier_pass_and_fail() -> None:
    verifier = FilesystemVerifier()
    good = await verifier.verify(
        Evidence(
            kind="filesystem",
            facts={"path": "a.txt", "exists": True, "hash_before": "h1", "hash_after": "h2"},
        )
    )
    assert good.verified is True
    assert good.summary == "2/2 checks passed"

    bad = await verifier.verify(
        Evidence(
            kind="filesystem",
            facts={"path": "a.txt", "exists": True, "hash_before": "h1", "hash_after": "h1"},
        )
    )
    assert bad.verified is False
    assert any(not c.passed for c in bad.checks)


async def test_terminal_verifier_exit_code_and_output() -> None:
    verifier = TerminalVerifier(expected_exit_code=0)
    ok = await verifier.verify(
        Evidence(kind="terminal", facts={"exit_code": 0, "stdout": "42 tests passed"})
    )
    assert ok.verified is True

    failed = await verifier.verify(
        Evidence(kind="terminal", facts={"exit_code": 1, "stdout": "Traceback"})
    )
    assert failed.verified is False


async def test_verifier_reports_every_check() -> None:
    verifier = TerminalVerifier()
    verdict = await verifier.verify(Evidence(kind="terminal", facts={"exit_code": 0, "stdout": ""}))
    assert len(verdict.checks) == 2
    assert verdict.verified is False
