"""Execution context: task-owned scope (SPEC §23 attribution, §46 isolation)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ExecutionContext:
    """Every action runs under its task. FULL_PC_AUTONOMY broadens scope but
    never removes attribution (SPEC §23)."""

    task_id: str
    workspace: Path
    #: owner-granted FULL_PC_AUTONOMY broadens filesystem scope beyond the
    #: task workspace; paths are still resolved and attributed.
    full_pc: bool = False

    @staticmethod
    def for_task(task_id: str, root: str | Path, *, full_pc: bool = False) -> ExecutionContext:
        workspace = Path(root) / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        return ExecutionContext(task_id=task_id, workspace=workspace, full_pc=full_pc)
