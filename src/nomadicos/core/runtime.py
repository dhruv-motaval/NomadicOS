"""Runtime composition root (BP Â§234, Â§237; ADR-0015/0023): wires all subsystems.

Persistence posture (BP Â§237): PostgreSQL is canonical; when the database is
reachable the runtime persists audit/experiences/memory and enables the
Network Gateway tools. When unreachable it degrades to in-memory stores
(never permissive â€” features that need persistence are simply unavailable).
"""

import asyncio
from pathlib import Path
from typing import Any

from nomadicos.agent.runtime import AgentRuntime, TaskReport
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.audit.base import AuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.config import CoreConfig, load_config
from nomadicos.core.errors import NomadicError
from nomadicos.core.events import EventBus
from nomadicos.core.lifecycle import Lifecycle
from nomadicos.core.logging import configure_logging, get_logger
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.memory.base import MemoryScope
from nomadicos.memory.engine import MemoryEngine
from nomadicos.models.base import ModelStatus
from nomadicos.models.manager import ModelManager
from nomadicos.network.gateway import NetworkGateway
from nomadicos.security.budgets import TaskBudget
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway
from nomadicos.tools.terminal import TerminalTool

logger = get_logger("core.runtime")


class Runtime:
    """Wires the subsystems; `run_goal` executes one task end-to-end (BP Â§78)."""

    # Repo root: resolved from this file (â€¦/src/nomadicos/core/runtime.py),
    # so the CLI works from ANY working directory (ADR-0015).
    REPO_ROOT = Path(__file__).resolve().parents[3]

    def __init__(self, config: CoreConfig | None = None) -> None:
        configure_logging()
        self.config = config or load_config(self.REPO_ROOT / "config" / "config.yaml")

        # --- Constitution (BP Â§4): policy load is fail-closed (ADR-0013).
        self.policy = PolicyEngine()
        policy_dir = self.REPO_ROOT / "config" / "policies"
        if policy_dir.exists():
            self.policy.load_directory(policy_dir)

        # --- Persistence: PostgreSQL canonical, in-memory degraded fallback
        # (BP Â§1.2, Â§237; ADR-0009/0010).
        self.db = None
        self._init_persistence()

        # --- Constitution enforcement stack (BP Â§98).
        self.permissions = PermissionEngine()
        self.gate = SecurityGate(self.policy, self.permissions, self.audit)
        self.manager = ModelManager(max_resident=2)
        self.lifecycle = Lifecycle(EventBus())

        # --- Models: default fake for CI; real Ollama fleet via
        # `register_ollama_models()` (ADR-0001, BP Â§148).
        from nomadicos.models.fake import FakeLocalModel

        self.manager.register(FakeLocalModel("fake/general"), status=ModelStatus.ENABLED)

        # --- Selection (BP Â§97/Â§320) + tools (BP Â§13).
        self.performance = ModelPerformanceTracker()
        self.selector = ModelSelector(
            self.manager,
            self.performance,
            hardware=HardwareConstraints(ram_mb=16384),
        )
        self.gateway = ToolGateway(self.gate, self.audit)
        self.workspace_root = str(self.config.paths.data_dir / "task-workspaces")
        Path(self.workspace_root).mkdir(parents=True, exist_ok=True)
        self.gateway.register(FilesystemTool(workspace_root=self.workspace_root))
        self.gateway.register(TerminalTool(workspace_root=self.workspace_root))
        # Growing toolbox: scripts the agent itself wrote in past tasks
        # (owner-vision: self-implementing; still gated, still audited).
        from nomadicos.tools.generated import load_generated_tools

        for generated in load_generated_tools(self.REPO_ROOT / "data" / "scripts"):
            self.gateway.register(generated)
        self._network_gateway: NetworkGateway | None = None

        # --- Memory (BP Â§16, Â§376-420): persistent when DB reachable.
        self.memory = self._init_memory()

        # --- Experience (BP Â§18) + Evaluation (BP Â§100).
        self.experience_store = self._experience_store()
        self.recorder = ExperienceRecorder(self.experience_store)
        self.evaluator = EvaluationEngine()
        from nomadicos.agent.machine_profile import ensure_profile
        from nomadicos.agent.skills import SkillStore

        self._skill_store = SkillStore(self.REPO_ROOT / "data" / "skills")
        try:
            self._machine_profile = ensure_profile(self.REPO_ROOT / "data")
        except Exception:  # noqa: BLE001 — profile is best effort, table still applies
            self._machine_profile = ""
        self._orchestration_enabled = False  # owner opt-in (ADR-0030 staged rollout)
        self._conversation: list[dict[str, Any]] = []  # session continuity (BP §376)
        self._emergency_stopped = False
        self._fleet: Any = None
        self._fleet_watch_task: Any = None

    @staticmethod
    def _looks_decomposable(goal: str) -> bool:
        """Cheap heuristic: multi-step goals read as action chains."""
        import re as _re

        action_hits = len(_re.findall(r"\band\b|,|then|after that|;", goal.lower()))
        return action_hits >= 2

    def _agent_factory(self, base_runtime: Any) -> Any:
        """Runtime factory for orchestrator roles: same subsystems, and each
        execute_task call re-selects the model for the (sub)goal via the
        selector agent â€” workers follow their subtask, planner/synthesizer get
        the reasoning tier (BP Â§364)."""

        def factory(agent_id: str, role: Any) -> Any:
            from nomadicos.agent.runtime import AgentRuntime

            runtime = AgentRuntime(
                selector=base_runtime._selector,
                manager=base_runtime._manager,
                gateway=base_runtime._gateway,
                audit_sink=base_runtime._audit,
                recorder=base_runtime._recorder,
                evaluator=base_runtime._evaluator,
                memory=base_runtime._memory,
                skills=base_runtime._skills,
                budget=base_runtime._budget_cfg,
            )
            original_execute = runtime.execute_task
            role_name = role.name

            async def execute_with_role_model(
                goal: str, identity: Any, **kwargs: Any
            ) -> Any:
                if kwargs.get("model_id") is None:
                    decision = (
                        await runtime.selector_agent.select(goal)
                        if role_name == "worker"
                        else await runtime.selector_agent.select_for_role(goal, role_name)
                    )
                    kwargs["model_id"] = decision.model_id
                return await original_execute(goal, identity, **kwargs)

            runtime.execute_task = execute_with_role_model  # type: ignore[method-assign]
            return runtime

        return factory

    # -------------------------------------------------------------- persistence

    def _init_persistence(self) -> None:
        self._load_dotenv()
        from nomadicos.audit.base import InMemoryAuditSink
        from nomadicos.experience.store import InMemoryExperienceStore

        self.audit: AuditSink = InMemoryAuditSink()
        self._experience_store_any: Any = InMemoryExperienceStore()
        self._pg_available = False
        self._pg_client: Any = None

        import os

        password = os.environ.get(self.config.postgres.password_env)
        if not password:
            logger.warning(
                "persistence degraded: %s not set (in-memory stores)",
                self.config.postgres.password_env,
            )
            return
        try:
            from nomadicos.postgres.client import client_from_config
            from nomadicos.postgres.migrator import MigrationRunner

            self._pg_client = client_from_config(self.config.postgres, password)
            self._pg_client.connect()
            if not self._pg_client.ping():
                raise RuntimeError("ping failed")
            MigrationRunner(
                self._pg_client, self.REPO_ROOT / "src/nomadicos/postgres/migrations"
            ).run()
        except Exception as exc:  # noqa: BLE001 â€” BP Â§237 degrade, never crash
            logger.warning(
                "persistence degraded: PostgreSQL unavailable (%s)", type(exc).__name__
            )
            self._pg_client = None
            return

        self._pg_available = True
        from nomadicos.postgres.audit_sink import PostgresAuditSink

        self.audit = PostgresAuditSink(self._pg_client)
        logger.info("persistence active: PostgreSQL %s", self.config.postgres.host)

    def _experience_store(self) -> Any:
        if self._pg_available:
            from nomadicos.postgres.experience_store import PostgresExperienceStore

            return PostgresExperienceStore(self._pg_client)
        return self._experience_store_any

    def _init_memory(self) -> MemoryEngine:
        if self._pg_available:
            from nomadicos.postgres.memory_store import PostgresMemoryStore

            return MemoryEngine(PostgresMemoryStore(self._pg_client))
        from nomadicos.memory.fake import FakeMemoryStore

        return MemoryEngine(FakeMemoryStore())

    # ------------------------------------------------------------------ models

    def register_model(self, model: Any, status: Any = None) -> None:
        self.manager.register(model, status=status or ModelStatus.ENABLED)

    async def register_ollama_models(self) -> int:
        """Discover + register the local Ollama fleet (BP Â§148, ADR-0001 alt).

        Once real models are registered, CI fakes are demoted (BP Â§148: real
        evidence wins; fakes are placeholders, BP Â§211)."""
        from nomadicos.models.ollama_adapter import OllamaModel

        try:
            fleet = await OllamaModel.discover()
        except NomadicError as exc:
            logger.warning("ollama discovery failed: %s", exc)
            return 0
        for model in fleet:
            try:
                self.register_model(model)
            except ValueError:
                continue  # already registered
        if fleet:
            for model_id in list(self.manager.snapshot()["status"]):
                if model_id.startswith("fake/"):
                    self.manager.set_status(model_id, ModelStatus.DISABLED)
                    logger.info("fake model disabled (real models available): %s", model_id)
        logger.info("ollama fleet registered count=%d", len(fleet))
        return len(fleet)

    async def sync_fleet(self, *, force: bool = True) -> dict[str, int]:
        """BP Â§8.4/Â§148: reconcile the model registry with the live Ollama server.

        Detects models the owner added, removed, or re-pulled with new weights,
        and adapts selection automatically (no restart)."""
        from nomadicos.models.fleet import FleetSynchronizer

        if self._fleet is None:
            self._fleet = FleetSynchronizer(self.manager, performance=self.performance)
        report = await self._fleet.sync(force=force)
        return {
            "added": len(report.added),
            "removed": len(report.removed),
            "changed": len(report.changed),
            "healthy": len(report.healthy),
            "failed": len(report.failed),
        }

    def start_fleet_watch(self, interval_minutes: float = 5.0) -> None:
        """BP Â§148: background fleet sync (new/removed/re-pulled models)."""
        import asyncio

        if self._fleet_watch_task is not None:
            return
        self._fleet_watch_task = asyncio.create_task(self._fleet_watch_loop(interval_minutes * 60))

    async def _fleet_watch_loop(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                counts = await self.sync_fleet()
                logger.info("fleet watch synced counts=%s", counts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 â€” never die on sync errors
                logger.warning("fleet watch failed: %s", type(exc).__name__)

    async def aclose(self) -> None:
        if self._fleet_watch_task is not None:
            self._fleet_watch_task.cancel()
            try:
                await self._fleet_watch_task
            except asyncio.CancelledError:
                pass
            self._fleet_watch_task = None
        if self._pg_client is not None:
            self._pg_client.close()

    # ------------------------------------------------------------------ network

    def enable_network(self) -> None:
        """BP Â§23-24/Â§48: register web.fetch behind the Network Gateway."""
        if self._network_gateway is not None:
            return
        from nomadicos.network.base import HttpxTransport

        self._network_gateway = NetworkGateway(
            self.gate, self.audit, HttpxTransport()
        )
        self.gateway.register(__import__(
            "nomadicos.network.web_tool", fromlist=["WebFetchTool"]
        ).WebFetchTool(self._network_gateway))
        logger.info("network gateway enabled (public GET only)")

    # ------------------------------------------------------------------ status

    def status_line(self) -> str:
        memory_count = "?"
        return (
            f"NomadicOS | env={self.config.environment} | "
            f"persistence={'postgresql' if self._pg_available else 'in-memory'} | "
            f"tools={self.gateway.registered_tools()} | "
            f"models={len(self.manager.list_available())} | "
            f"memory={memory_count} | "
            f"emergency_stopped={self._emergency_stopped}"
        )

    def emergency_stop(self) -> None:
        self._emergency_stopped = True

    # ----------------------------------------------------------------- task run

    def recent_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        """Recent task history for the CLI /sessions view (BP Â§206)."""
        if not self._pg_available:
            return []
        return self._pg_client.execute(
            "SELECT created_at, status, goal FROM nomadicos.tasks "
            "ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    def run_goal_sync(self, goal: str, *, user_id: str = "local-owner") -> TaskReport:
        """Sync wrapper for CLI callers."""
        return asyncio.run(self.run_goal(goal, user_id=user_id))

    async def run_goal(self, goal: str, *, user_id: str = "local-owner") -> TaskReport:
        """BP Â§78: user task â†’ memory context â†’ model selection â†’ tools â†’
        verify â†’ experience â†’ report."""
        from nomadicos.agent.runtime import TaskReport
        from nomadicos.core.lifecycle import TaskStatus
        from nomadicos.security.permissions import SubjectIdentity

        if self._emergency_stopped:
            return TaskReport(
                task_id="halted",
                goal=goal,
                status=TaskStatus.CANCELLED,
                requested=goal,
                failed=["emergency stop active"],
            )

        memory_engine = self.memory
        memory_context = await memory_engine.search(goal, limit=3) if goal.strip() else []

        # Persist the session + task (BP Â§19, Â§206) so experiences/audit FKs hold.
        session_id: str | None = None
        task_id: str | None = None
        if self._pg_available:
            try:
                from nomadicos.postgres.repositories import SessionRepository, TaskRepository

                sessions = SessionRepository(self._pg_client)
                tasks = TaskRepository(self._pg_client)
                session_id = str(await sessions.create(user_id, title=goal[:120]))
                import uuid as uuid_mod

                task_id = str(
                    await tasks.create(
                        user_id, goal, session_id=uuid_mod.UUID(session_id)
                    )
                )
            except Exception as exc:  # noqa: BLE001 â€” degrade (BP Â§237)
                logger.warning("task persistence degraded: %s", type(exc).__name__)

        runtime = AgentRuntime(
            selector=self.selector,
            manager=self.manager,
            gateway=self.gateway,
            audit_sink=self.audit,
            recorder=self.recorder,
            evaluator=self.evaluator,
            memory=memory_engine,
            memory_context=memory_context,
            budget=TaskBudget(max_steps=8),
            skills=self._skill_store,
            machine_profile=self._machine_profile,
            workspace_root=self.workspace_root,
            conversation=self._conversation[-3:],
        )
        identity = SubjectIdentity(user_id=user_id, session_id=session_id, task_id=task_id)

        # Multi-agent orchestration (ADR-0030): planner â†’ waves â†’ synthesizer,
        # each agent with its own model selection (BP Â§364). Only for goals
        # that actually decompose (â‰¥2 action verbs / conjunctions); everything
        # else runs single-agent â€” cheaper and equally verified.
        if self._orchestration_enabled and self._looks_decomposable(goal):
            from nomadicos.agent.orchestrator import Orchestrator

            planner_decision = await runtime.selector_agent.select_for_role(goal, "planner")
            planner_model = await runtime.handler_agent.ensure_model(
                planner_decision.model_id
            )
            orchestrator = Orchestrator(
                planner=planner_model,
                audit_sink=self.audit,
                runtime_factory=self._agent_factory(runtime),
            )
            result = await orchestrator.orchestrate(goal, identity)
            from nomadicos.agent.runtime import TaskReport

            return TaskReport(
                task_id=task_id or "orchestrated",
                goal=goal,
                status=result.final_status,  # type: ignore[arg-type]
                requested=goal,
                completed=[r.description for r in result.subtask_results],
                failed=[r.error for r in result.subtask_results if r.error],
                duration_seconds=round(result.duration_seconds, 2),
                reply=result.synthesis,
            )

        report = await runtime.execute_task(goal, identity)

        # Session continuity (BP §376): remember this exchange so follow-ups
        # ("give me the path of that file") understand the reference.
        self._conversation.append(
            {
                "goal": goal[:200],
                "status": report.status.value,
                "completed": report.completed[:2],
                "reply": (report.reply or "")[:150],
            }
        )
        del self._conversation[:-8]

        # Persist task-level memory (BP Â§166: source + scope always present).
        summary = f"{goal[:120]} -> {report.status.value}"
        await memory_engine.store(
            summary,
            scope=MemoryScope.PROJECT,
            source=f"agent-runtime ({report.status.value})",
            session_id=identity.session_id if hasattr(identity, "session_id") else None,
            confidence=0.9 if report.status.value == "SUCCESS" else 0.4,
            verified=any("2/2" in v or "1/1" in v for v in report.verification),
        )
        return report


    @staticmethod
    def _load_dotenv() -> None:
        """Minimal .env loader (repo root) â€” sets only unset variables (BP Â§103)."""
        import os

        path = Runtime.REPO_ROOT / ".env"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            # Strip inline comments (a ` #` sequence starts a comment).
            if " #" in value:
                value = value.split(" #", 1)[0].strip()
            os.environ.setdefault(key.strip(), value.strip("'").strip('"'))

__all__ = ["Runtime"]
