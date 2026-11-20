"""TaskRuntime: injected services + the ONLY AuthorizedAction table.

LangGraph state may reference issued artifacts by id, but the artifact object
itself lives here — a value smuggled into state cannot execute anything
(SPEC §7.11 "graph cannot manufacture AuthorizedAction").
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nomadicos.action_ir.validation import ProposalValidator
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.action import ActionProposal, AuthorizedAction
from nomadicos.evaluation.benchmarking import TaskRunOutcome  # noqa: F401 (typing aid)
from nomadicos.executor.dispatch import Executor
from nomadicos.inference.base import GenerationRequest, InferenceEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.errors import ResourceUnavailable
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.boundaries import GoalVerifier, StepVerifier
from nomadicos.orchestration.planner import Planner
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.escalation import EscalationPolicy
from nomadicos.router.selection import ModelSelector
from nomadicos.tools.base import ToolRegistry

#: filesystem operations that change files, and the args naming what they change
_WRITE_OPS = {
    "write": ("path",),
    "create": ("path",),
    "delete": ("path",),
    "mkdir": ("directory",),
    "move": ("source", "target"),
}


def _write_keys(proposal: ActionProposal, workspace: Path) -> list[str]:
    """Resolved, case-normalized paths a filesystem change touches.

    shortcut: terminal commands that write files are not leased (their targets
    are not known before they run); use filesystem ops for shared files.
    """
    if proposal.tool != "filesystem":
        return []
    keys: list[str] = []
    for arg in _WRITE_OPS.get(proposal.operation, ()):
        raw = proposal.args.get(arg)
        if isinstance(raw, str) and raw.strip():
            try:
                keys.append(os.path.normcase(str((workspace / raw).resolve(strict=False))))
            except (OSError, ValueError):
                continue  # the tool's own path checks reject it
    return sorted(set(keys))  # one order for everyone: no lock-order inversion within an action


#: prompt/context strategy: (state, step_description, goal, model_id, cfg,
#: catalog) -> GenerationRequest. Workers specialize this; security does not.
ContextBuilder = Callable[..., GenerationRequest]


@dataclass
class TaskRuntime:
    config: AppConfig
    logger: EventLogger
    store: AuthorityStore
    authz: AuthorizationService
    validator: ProposalValidator
    tools: ToolRegistry
    executor: Executor
    registry: ModelRegistry
    selector: ModelSelector
    escalator: EscalationPolicy
    planner: Planner
    engines: dict[str, InferenceEngine]
    workspace_root: Path
    default_engine: str = "mock"
    step_verifier: StepVerifier | None = None
    goal_verifier: GoalVerifier | None = None
    full_pc: bool = False
    #: when False, the workspace_root itself is the task workspace (a coding
    #: repository designated by the owner - SPEC §9.5)
    workspace_per_task: bool = True
    #: optional prompt/context strategy (worker specialization), §9.24
    context_builder: ContextBuilder | None = None
    #: optional Critic (SPEC §10): evaluator only - no authority, no execution
    critic: Any = None
    #: optional memory service (Phase 11G): DATA-only context + read seams;
    #: it can never authorize, execute, or route (memory is not authority)
    memory: Any = None
    #: Phase 1 brain integration: when True the propose node runs the
    #: NomadicBrain Actor<->Monitor pipeline for non-chat steps and routes
    #: accepted candidates through the SAME untrusted parse/validate/
    #: authorize path as raw model output (see brain_adapter).
    brain_actor: bool = False
    #: Phase 4A: when True NomadicBrain proposes the mission task graph and
    #: the runtime schedules eligible tasks one at a time. Legacy planner
    #: path unchanged when False.
    brain_planner_enabled: bool = False
    _issued: dict[str, AuthorizedAction] = field(default_factory=dict, repr=False)
    #: the desktop (keyboard, mouse, focus) is ONE shared resource: exactly one
    #: task holds the lease at a time, from its first desktop action until its
    #: mission ends. Every other tool class is unaffected (SPEC §46).
    _desktop_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _desktop_owner: str | None = field(default=None, repr=False)
    #: file write leases: resolved path -> lock / owning task id
    _write_locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    _write_owner: dict[str, str] = field(default_factory=dict, repr=False)

    async def acquire_desktop(self, task_id: str) -> None:
        """Re-entrant per task; waits up to ``budget.desktop_lease_timeout_s``."""
        if self._desktop_owner == task_id:
            return
        try:
            await asyncio.wait_for(
                self._desktop_lock.acquire(), self.config.budget.desktop_lease_timeout_s
            )
        except TimeoutError:
            raise ResourceUnavailable(
                "desktop is in use by another task", holder=self._desktop_owner
            ) from None
        self._desktop_owner = task_id

    def release_desktop(self, task_id: str) -> None:
        if self._desktop_owner == task_id:
            self._desktop_owner = None
            self._desktop_lock.release()
        self._release_writes(task_id)

    async def acquire_for(self, proposal: ActionProposal, task_id: str, workspace: Path) -> None:
        """Take the exclusive resources an action needs before it runs: the
        desktop, or the files a filesystem change touches. A file stays leased
        to the agent that first changed it until that agent's run ends, so two
        agents never interleave edits to one file (SPEC §46)."""
        if proposal.tool == "desktop":
            await self.acquire_desktop(task_id)
        for key in _write_keys(proposal, workspace):
            await self._acquire_write(task_id, key)

    async def _acquire_write(self, task_id: str, key: str) -> None:
        if self._write_owner.get(key) == task_id:
            return
        lock = self._write_locks.setdefault(key, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), self.config.budget.write_lease_timeout_s)
        except TimeoutError:
            holder = self._write_owner.get(key)
            # hold-and-wait: give up this agent's other leases so the agent it is
            # deadlocked with can finish; this action fails and is retried
            self._release_writes(task_id)
            raise ResourceUnavailable(
                f"{key} is being changed by another agent", holder=holder
            ) from None
        self._write_owner[key] = task_id

    def _release_writes(self, task_id: str) -> None:
        for key, owner in list(self._write_owner.items()):
            if owner == task_id:
                del self._write_owner[key]
                self._write_locks[key].release()

    # ------------------------------------------- issued-artifact table ---
    def issue(self, authorized: AuthorizedAction) -> str:
        self._issued[authorized.id] = authorized
        return authorized.id

    def take(self, action_ref: str | None) -> AuthorizedAction | None:
        """Pop semantics: an authorized artifact is consumable exactly once
        by the graph; re-use needs a fresh authorize (SPEC §7.20)."""
        if action_ref is None:
            return None
        return self._issued.pop(action_ref, None)

    def issued_count(self) -> int:
        return len(self._issued)

    def engine_for(self, model_id: str) -> InferenceEngine:
        try:
            record = self.registry.get(model_id)
        except Exception:
            record = None
        name = record.engine if record else self.default_engine
        engine = self.engines.get(name) or self.engines.get(self.default_engine)
        if engine is None:
            raise KeyError(f"no engine brick named {name!r} is wired")
        return engine
