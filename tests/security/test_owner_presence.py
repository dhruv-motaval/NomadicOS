"""Owner presence before widening authority (SPEC §4, §5, §56.5).

A task process runs as the owner's OS user and, with desktop control, can
type into the owner's own terminal. Widening decisions therefore need the
owner in person (Windows Hello); restricting ones never wait. The real OS
prompt is exercised by hand; these tests pin the brick's contract and every
surface that must ask.
"""

from __future__ import annotations

import base64
import importlib
import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nomadicos.authority import AuthorityStore
from nomadicos.authority.presence import (
    NoPresenceCheck,
    OwnerPresenceNotConfirmed,
    PresenceResult,
    PresenceVerdict,
    WindowsHelloPresence,
    presence_from_config,
    require_owner_presence,
)
from nomadicos.kernel.config import AppConfig

WINDOWS = os.name == "nt"
#: the module (the package re-exports the Typer app under the same name)
cli_module = importlib.import_module("nomadicos.cli.app")


class FakePresence:
    def __init__(self, verdict: PresenceVerdict = PresenceVerdict.VERIFIED) -> None:
        self.verdict = verdict
        self.prompts: list[str] = []

    def confirm(self, purpose: str) -> PresenceResult:
        self.prompts.append(purpose)
        return PresenceResult(self.verdict, "fake")


def hello_with(output: str | Exception) -> tuple[WindowsHelloPresence, list[list[str]]]:
    calls: list[list[str]] = []

    def runner(argv: list[str], timeout_s: float) -> str:
        calls.append(argv)
        if isinstance(output, Exception):
            raise output
        return output

    return WindowsHelloPresence(timeout_s=5.0, runner=runner), calls


# --------------------------------------------------------------- brick ----


@pytest.mark.skipif(not WINDOWS, reason="Windows Hello is Windows-only")
@pytest.mark.parametrize(
    "output,verdict",
    [
        ("Verified", PresenceVerdict.VERIFIED),
        ("Canceled", PresenceVerdict.DECLINED),
        ("RetriesExhausted", PresenceVerdict.DECLINED),
        ("NotConfiguredForUser", PresenceVerdict.UNAVAILABLE),
        ("DeviceNotPresent", PresenceVerdict.UNAVAILABLE),
        ("Timeout", PresenceVerdict.UNAVAILABLE),
        ("", PresenceVerdict.UNAVAILABLE),
        ("verified", PresenceVerdict.UNAVAILABLE),  # exact match only
    ],
)
def test_hello_results_map_fail_closed(output: str, verdict: PresenceVerdict) -> None:
    presence, _ = hello_with(output)
    assert presence.confirm("grant FULL_PC_AUTONOMY").verdict is verdict


@pytest.mark.skipif(not WINDOWS, reason="Windows Hello is Windows-only")
@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("powershell", 5.0), OSError("cannot spawn")]
)
def test_a_prompt_that_cannot_run_is_unavailable(error: Exception) -> None:
    presence, _ = hello_with(error)
    assert presence.confirm("x").verdict is PresenceVerdict.UNAVAILABLE


@pytest.mark.skipif(not WINDOWS, reason="Windows Hello is Windows-only")
def test_hello_runs_the_system_powershell_without_profile_or_cmdlets() -> None:
    presence, calls = hello_with("Verified")
    presence.confirm("grant FULL_PC_AUTONOMY'; Write-Output Verified; '")
    (argv,) = calls
    exe = Path(argv[0])
    assert exe.is_absolute() and exe.name.lower() == "powershell.exe"
    assert exe.parent.parent.parent.name.lower() == "system32"
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(argv[4]).decode("utf-16-le")
    # the purpose travels base64-encoded: quotes in it cannot become code
    assert "Write-Output" not in script
    encoded = re.search(r"\$b64 = '([A-Za-z0-9+/=]+)'", script)
    assert encoded is not None
    assert base64.b64decode(encoded.group(1)).decode().startswith("NomadicOS: grant")
    # no cmdlets: they would autoload from a user-writable module path
    assert not re.search(r"\b(Add-Type|Where-Object|Select-Object|Import-Module)\b", script)


def test_require_raises_unless_verified() -> None:
    assert require_owner_presence(FakePresence(), "x").verified
    for verdict in (PresenceVerdict.DECLINED, PresenceVerdict.UNAVAILABLE):
        with pytest.raises(OwnerPresenceNotConfirmed):
            require_owner_presence(FakePresence(verdict), "x")


def test_config_selects_the_brick_and_defaults_to_hello() -> None:
    assert AppConfig().autonomy.owner_presence == "windows_hello"
    assert isinstance(presence_from_config("windows_hello"), WindowsHelloPresence)
    assert isinstance(presence_from_config("none"), NoPresenceCheck)
    with pytest.raises(Exception, match="owner_presence"):
        AppConfig.model_validate({"autonomy": {"owner_presence": "trust_me"}})


# ----------------------------------------------------------- owner CLI ----


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    presence = FakePresence()
    monkeypatch.setattr(cli_module, "_owner_presence", lambda cfg: presence)
    store = AuthorityStore(tmp_path / "data" / "state" / "authority.json")
    return CliRunner(), presence, store


def test_cli_grant_needs_the_owner_in_person(cli) -> None:
    runner, presence, store = cli
    presence.verdict = PresenceVerdict.DECLINED
    result = runner.invoke(cli_module.app, ["permissions", "grant-full"])
    assert result.exit_code == 3
    assert store.has_full_autonomy is False
    presence.verdict = PresenceVerdict.VERIFIED
    assert runner.invoke(cli_module.app, ["permissions", "grant-full"]).exit_code == 0
    assert store.has_full_autonomy is True
    assert presence.prompts == ["grant FULL_PC_AUTONOMY"] * 2


def test_cli_allow_answer_needs_presence_deny_does_not(cli) -> None:
    runner, presence, store = cli
    store.record_conflict("req-1", {"fingerprint": "fp-1"})
    store.record_conflict("req-2", {"fingerprint": "fp-2"})
    presence.verdict = PresenceVerdict.UNAVAILABLE
    result = runner.invoke(cli_module.app, ["permissions", "answer", "req-1", "allow"])
    assert result.exit_code == 3
    assert "req-1" in store.state().conflicts
    deny = runner.invoke(cli_module.app, ["permissions", "answer", "req-2", "DENY"])
    assert deny.exit_code == 0
    assert "fp-2" in store.state().denied_fingerprints
    assert runner.invoke(cli_module.app, ["permissions", "revoke-all"]).exit_code == 0
    assert presence.prompts == ["ALLOW conflict req-1"]


def test_typed_allow_counts_only_after_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive OWNER DECISION prompt: a typed ALLOW (which desktop
    control could produce) is not enough on its own."""
    presence = FakePresence(PresenceVerdict.DECLINED)
    monkeypatch.setattr(cli_module, "_owner_presence", lambda cfg: presence)
    answers = iter(["ALLOW", "maybe", "DENY"])
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *a, **k: next(answers))
    assert cli_module._prompt_owner_decision(AppConfig(), "ALLOW x") == "DENY"
    assert presence.prompts == ["ALLOW x"]

    presence.verdict = PresenceVerdict.VERIFIED
    answers = iter(["allow"])
    assert cli_module._prompt_owner_decision(AppConfig(), "ALLOW y") == "ALLOW"
