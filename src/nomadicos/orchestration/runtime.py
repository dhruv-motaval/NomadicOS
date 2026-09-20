"""TaskRuntime: injected services + the ONLY AuthorizedAction table.

LangGraph state may reference issued artifacts by id, but the artifact object
itself lives here — a value smuggled into state cannot execute anything
(SPEC §7.11 "graph cannot manufacture AuthorizedAction").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nomadicos.action_ir.validation import ProposalValidator
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.action import AuthorizedAction
from nomadicos.evaluation.benchmarking import TaskRunOutcome  # noqa: F401 (typing aid)
from nomadicos.executor.dispatch import Executor
from nomadicos.inference.base import GenerationRequest, InferenceEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.boundaries import GoalVerifier, StepVerifier
from nomadicos.orchestration.planner import Planner
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.router.escalation import EscalationPolicy
from nomadicos.router.selection import ModelSelector
from nomadicos.tools.base import ToolRegistry

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
    _issued: dict[str, AuthorizedAction] = field(default_factory=dict, repr=False)

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
