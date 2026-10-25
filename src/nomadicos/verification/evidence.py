"""Evidence model (SPEC §8.6-8.7, §8.21).

EvidenceContext is what a verifier READS. It carries task-correlated
execution evidence (already produced by the executor) plus a read-only view
of the filesystem. Nothing in here mutates anything (SPEC §8.33).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nomadicos.contracts.execution import ExecutionResult

MAX_READ_BYTES = 1_000_000


@dataclass
class EvidenceContext:
    #: the authoritative goal identity; every correlated query filters on it
    task_id: str
    workspace: Path | None = None
    executions: list[ExecutionResult] = field(default_factory=list)
    budget_items: int = 60
    _used: int = 0

    # ------------------------------------------------------- correlation --
    def task_executions(self, step_id: str | None = None) -> list[ExecutionResult]:
        """Evidence from OTHER tasks can never satisfy this one (SPEC §8.21)."""
        out = [e for e in self.executions if e.task_id == self.task_id]
        if step_id:
            out = [e for e in out if e.step_id == step_id]
        return out

    # ------------------------------------------------------------ files ---
    def resolve(self, raw: str | Path) -> Path:
        p = Path(str(raw))
        if not p.is_absolute() and self.workspace is not None:
            p = self.workspace / p
        return Path(p).resolve(strict=False)

    def file_bytes(self, raw: str | Path) -> tuple[bytes | None, str | None]:
        """Read-only. Returns (content, error). Never creates or fixes."""
        path = self.resolve(raw)
        try:
            if not path.is_file():
                return None, None
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                return None, f"file too large to verify ({size} bytes; limit {MAX_READ_BYTES})"
            with open(path, "rb") as fh:
                return fh.read(), None
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def file_sha256(self, raw: str | Path) -> tuple[str | None, str | None]:
        data, error = self.file_bytes(raw)
        if error:
            return None, error
        if data is None:
            return None, None
        return hashlib.sha256(data).hexdigest(), None

    def exists(self, raw: str | Path, *, directory: bool = False) -> bool:
        path = self.resolve(raw)
        return path.is_dir() if directory else path.is_file()

    # ------------------------------------------------------------ spend --
    def charge(self, n: int = 1) -> bool:
        """Bounded evaluation (SPEC §8.34): returns False once exhausted."""
        if self._used + n > self.budget_items:
            return False
        self._used += n
        return True

    # --------------------------------------------------- execution match --
    def find_execution(
        self,
        *,
        command: str | None = None,
        step_id: str | None = None,
        tool: str | None = None,
        operation: str | None = None,
    ) -> list[ExecutionResult]:
        execs = self.task_executions(step_id=step_id)
        if tool is not None:
            execs = [e for e in execs if e.tool == tool]
        if operation is not None:
            execs = [e for e in execs if e.operation == operation]
        if command is not None:
            execs = [
                e
                for e in execs
                if str(e.evidence.get("command", "")) == command
                or command
                == " ".join(
                    [str(e.evidence.get("command", "")), *map(str, e.evidence.get("args", []))]
                ).strip()
            ]
        return execs


def as_context(
    task_id: str,
    executions: list[ExecutionResult] | None,
    workspace: Path | str | None,
    extra: dict[str, Any] | None = None,
) -> EvidenceContext:
    return EvidenceContext(
        task_id=task_id,
        workspace=Path(workspace) if workspace else None,
        executions=list(executions or []),
        **(extra or {}),
    )
