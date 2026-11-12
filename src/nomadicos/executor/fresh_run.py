"""Verifier-side fresh execution of owner-authored predicate commands (§27).

The TRUSTED goal verifier may execute a completion-condition command itself
when no fresh executor evidence exists for it. This is the strongest
independent goal proof the system can obtain: the predicate command comes
from the OWNER's canonical goal (never model output), the run happens in the
verified workspace, and the result is plain executor evidence — the same
shape, always attributable, always bounded. The actual subprocess lives in
the executor brick (§8.33 keeps the verification brick side-effect-free);
the verifier only ever holds the injected callable.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from nomadicos.contracts.execution import ExecutionResult, ExecutionStatus
from nomadicos.kernel.interpreter import runtime_python_shell_command

FRESH_RUN_TIMEOUT_S = 600.0


def fresh_command_evidence(
    command: str,
    *,
    task_id: str,
    workspace: Path | None,
    timeout_s: float = FRESH_RUN_TIMEOUT_S,
) -> ExecutionResult:
    """Run one owner-authored predicate command and wrap the result as
    executor evidence. Exit code is the truth; the verifier invents nothing."""
    started_at = datetime.now(UTC)
    cwd = str(workspace.resolve()) if workspace is not None else None
    # a bare "python" resolves to the RUNTIME's interpreter (the project
    # venv with the owner's dependencies), never an arbitrary PATH pick
    resolved = runtime_python_shell_command(command)
    try:
        proc = subprocess.run(  # noqa: S602 - owner-authored completion command
            resolved,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        exit_code: int | None = proc.returncode
        stdout = (proc.stdout or "")[-4000:]
        stderr = (proc.stderr or "")[-4000:]
        status = ExecutionStatus.SUCCEEDED if proc.returncode == 0 else ExecutionStatus.FAILED
        message = ""
    except subprocess.TimeoutExpired:
        exit_code = None
        stdout, stderr = "", f"verifier: command timed out after {timeout_s:.0f}s"
        status = ExecutionStatus.FAILED
        message = "verifier fresh-run timed out"
    except OSError as exc:
        exit_code = None
        stdout, stderr = "", f"{type(exc).__name__}: {exc}"
        status = ExecutionStatus.FAILED
        message = "verifier fresh-run failed to start"
    return ExecutionResult(
        task_id=task_id,
        step_id="goal_verification_fresh",
        action_id="verifier",
        action_fingerprint=f"verifier:{command[:64]}",
        model_id="nomadic-goal-verifier",
        tool="terminal",
        operation="execute",
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        evidence={"command": command, "args": [], "runner": "goal-verifier-fresh"},
        message=message,
        started_at=started_at,
    )
