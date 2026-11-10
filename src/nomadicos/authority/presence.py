"""Owner presence (SPEC §4, §5, §56.5): proof that a WIDENING decision came
from the owner in person, not from a process acting as the owner.

Every owner surface runs as the owner's OS user, and so does every task
process. A token or a console prompt therefore cannot tell them apart: a task
with desktop control can type ``ALLOW`` into the owner's terminal. Widening
decisions (the FULL_PC grant, ALLOW answers, removing an owner rule) ask the
OS for the owner instead. Windows Hello's consent prompt is system UI that
only the owner's PIN or biometric satisfies, and a model does not know the
PIN. Restricting decisions (revoke, DENY, adding a rule) never wait on
presence: revocation must stay instant.

The Windows check runs System32 PowerShell 5.1 (located through the Win32
API, never PATH or an environment variable) with no profile and a
cmdlet-free script, so no user-writable module or profile can stand in for
the system prompt.
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from nomadicos.kernel.errors import AuthorizationDenied

#: bounded wait for the owner to answer the system prompt (SPEC §14)
PRESENCE_TIMEOUT_S = 120.0
#: the prompt text is shown by the OS; keep it short and owner-authored
MAX_PURPOSE_CHARS = 160


class PresenceVerdict(StrEnum):
    VERIFIED = "VERIFIED"
    #: the owner cancelled, or the PIN/biometric failed
    DECLINED = "DECLINED"
    #: no way to ask (not Windows, Hello not set up, timeout): fail closed
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PresenceResult:
    verdict: PresenceVerdict
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict is PresenceVerdict.VERIFIED


class OwnerPresenceNotConfirmed(AuthorizationDenied):
    """A widening owner decision without proof the owner made it."""


class OwnerPresence(Protocol):
    def confirm(self, purpose: str) -> PresenceResult: ...


class NoPresenceCheck:
    """``autonomy.owner_presence: none``: trusts whoever drives the surface.
    An explicit owner choice in config, never a silent fallback."""

    def confirm(self, purpose: str) -> PresenceResult:
        return PresenceResult(PresenceVerdict.VERIFIED, "owner_presence=none: not checked")


#: UserConsentVerificationResult -> verdict; anything unlisted is UNAVAILABLE
_HELLO_VERDICTS = {
    "Verified": PresenceVerdict.VERIFIED,
    "Canceled": PresenceVerdict.DECLINED,
    "RetriesExhausted": PresenceVerdict.DECLINED,
}

# Language constructs and .NET calls only: a cmdlet would autoload from
# PSModulePath, which includes a user-writable folder.
_HELLO_SCRIPT = """
$ErrorActionPreference = 'Stop'
$rt = 'System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, '
[void][System.Reflection.Assembly]::Load($rt + 'PublicKeyToken=b77a5c561934e089')
$asTask = $null
foreach ($m in [System.WindowsRuntimeSystemExtensions].GetMethods()) {
  $ps = $m.GetParameters()
  if ($m.Name -eq 'AsTask' -and $ps.Count -eq 1 -and
      $ps[0].ParameterType.Name -eq 'IAsyncOperation`1') { $asTask = $m; break }
}
$ns = 'Windows.Security.Credentials.UI'
$verifier = [type]($ns + '.UserConsentVerifier, ' + $ns + ', ContentType=WindowsRuntime')
$b64 = '__MESSAGE__'
$msg = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64))
$op = $verifier::RequestVerificationAsync($msg)
$resultType = [type]($ns + '.UserConsentVerificationResult')
$task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
[void]$task.Wait(__TIMEOUT_MS__)
if ($task.IsCompleted) { $out = [string]$task.Result } else { $out = 'Timeout' }
[Console]::Out.Write($out)
"""


def _system_directory() -> Path | None:
    """System32 from the Win32 API: an environment variable or PATH entry
    could be redirected by any process running as the owner."""
    if os.name != "nt":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    return Path(buffer.value) if 0 < length < len(buffer) else None


Runner = Callable[[list[str], float], str]


def _run_powershell(argv: list[str], timeout_s: float) -> str:
    system = _system_directory()
    env = {"SystemRoot": str(system.parent)} if system is not None else {}
    completed = subprocess.run(  # noqa: S603 - fixed system binary, fixed script
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
        check=False,
    )
    return completed.stdout.strip()


class WindowsHelloPresence:
    """Asks Windows Hello (PIN / fingerprint / face) to confirm the owner."""

    def __init__(self, *, timeout_s: float = PRESENCE_TIMEOUT_S, runner: Runner | None = None):
        self._timeout_s = timeout_s
        self._runner = runner or _run_powershell

    def confirm(self, purpose: str) -> PresenceResult:
        system = _system_directory()
        if system is None:
            return PresenceResult(PresenceVerdict.UNAVAILABLE, "Windows Hello needs Windows")
        powershell = system / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        message = f"NomadicOS: {purpose}"[:MAX_PURPOSE_CHARS]
        script = _HELLO_SCRIPT.replace(
            "__MESSAGE__", base64.b64encode(message.encode("utf-8")).decode("ascii")
        ).replace("__TIMEOUT_MS__", str(int(self._timeout_s * 1000)))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        argv = [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        try:
            # a little headroom so PowerShell reports its own timeout first
            outcome = self._runner(argv, self._timeout_s + 15.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return PresenceResult(PresenceVerdict.UNAVAILABLE, f"prompt failed: {exc}"[:200])
        verdict = _HELLO_VERDICTS.get(outcome, PresenceVerdict.UNAVAILABLE)
        return PresenceResult(verdict, f"windows hello: {outcome or 'no result'}"[:200])


def presence_from_config(mode: str) -> OwnerPresence:
    """``autonomy.owner_presence`` -> the presence brick."""
    if mode == "none":
        return NoPresenceCheck()
    return WindowsHelloPresence()


def require_owner_presence(presence: OwnerPresence, purpose: str) -> PresenceResult:
    """Raise unless the owner confirmed ``purpose`` in person."""
    result = presence.confirm(purpose)
    if not result.verified:
        raise OwnerPresenceNotConfirmed(
            f"owner presence not confirmed ({result.verdict.value}): {result.detail}",
            purpose=purpose,
        )
    return result
