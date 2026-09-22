"""Application service over the orchestration brick (SPEC §39, §41).

CLI and tests drive the SAME production path. Builds engines/registry per
configuration, exposes run/resume, and never touches authority directly
(except via the owner surfaces on the authority service).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.types import Command

from nomadicos.action_ir.validation import ProposalValidator
from nomadicos.authority.authorization import AuthorizationService
from nomadicos.authority.policy import CapabilityPolicy
from nomadicos.authority.store import AuthorityStore
from nomadicos.contracts.core import Goal, TaskStatus
from nomadicos.contracts.verification import VerificationLevel
from nomadicos.executor.dispatch import Executor
from nomadicos.inference.base import InferenceEngine
from nomadicos.inference.llama_cpp import LlamaCppEngine
from nomadicos.inference.mock import MockEngine
from nomadicos.inference.ollama import OllamaEngine
from nomadicos.kernel.config import AppConfig
from nomadicos.kernel.events import EventLogger, EventType
from nomadicos.memory.runtime import build_runtime_memory
from nomadicos.orchestration import memory_hooks
from nomadicos.orchestration.checkpointing import make_durable_saver
from nomadicos.orchestration.graph import build_graph
from nomadicos.orchestration.planner import Planner, StructuralPlanner
from nomadicos.orchestration.runtime import TaskRuntime
from nomadicos.persistence import TaskRecord, make_task_store
from nomadicos.persistence.contracts import AuthorizedActionRef, new_record, now_iso
from nomadicos.persistence.errors import PersistenceCorrupt, PersistenceError
from nomadicos.persistence.ledger import make_execution_ledger
from nomadicos.registry.model_registry import ModelRegistry
from nomadicos.registry.scanner import RegistryBuilder
from nomadicos.router.escalation import EscalationPolicy
from nomadicos.router.selection import ModelSelector
from nomadicos.tools.base import ToolRegistry
from nomadicos.tools.desktop import DesktopTool
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.terminal import ProcessSupervisor, TerminalTool


@dataclass
class RunSummary:
    task_id: str
    status: TaskStatus
    outcome_note: str = ""
    model_id: str | None = None
    steps_used: int = 0
    executions: int = 0
    recoveries: int = 0
    conflict: dict[str, Any] | None = None
    goal_verdict: str | None = None
    goal_why: list[str] = field(default_factory=list)
    goal_verifier: str | None = None
    critic_iterations: list[dict[str, Any]] = field(default_factory=list)
    critic_feedback_present: bool = False
    last_observations: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    #: Phase 11G observability: episodic/working memory result metadata only;
    #: it never changes task success semantics
    memory_note: str = ""

    def waiting_owner(self) -> bool:
        return self.status is TaskStatus.WAITING_OWNER


class NomadicApp:
    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        extra_engines: dict[str, InferenceEngine] | None = None,
        registry: ModelRegistry | None = None,
        planner: Planner | None = None,
        workspace_root: str | Path | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self.config = config or AppConfig()
        cfg = self.config
        self.log = EventLogger()
        self.store = AuthorityStore(Path(state_dir or cfg.persistence.state_dir) / "authority.json")
        engines: dict[str, InferenceEngine] = {
            "llamacpp": LlamaCppEngine(
                base_url=cfg.inference.llama_server_url,
                default_timeout_s=cfg.inference.request_timeout_s,
            ),
            "ollama": OllamaEngine(
                base_url=cfg.inference.ollama_base_url,
                default_timeout_s=cfg.inference.request_timeout_s,
            ),
            "mock": MockEngine(default_response='{"finished": true}'),
        }
        if extra_engines:
            engines.update(extra_engines)
        self.engines = engines
        self.registry = registry or ModelRegistry(self.log)
        self.tools = ToolRegistry()
        self.supervisor = ProcessSupervisor()
        self.tools.register(FilesystemTool())
        self.tools.register(TerminalTool(self.supervisor))
        # Phase 12A: desktop capability behind the same executor boundary;
        # platform adapter selected by backend.default_backend() (win32 here,
        # honest-unavailable elsewhere). Registration only — the tool gains
        # no authority and runs only on AuthorizedAction artifacts.
        self.tools.register(DesktopTool())
        self.validator = ProposalValidator(self.tools, self.log)
        self.policy = CapabilityPolicy(
            self.store,
            granted_patterns=list(cfg.autonomy.capabilities),
            hard_denied_resources=list(cfg.autonomy.denied_resources),
        )
        self.authz = AuthorizationService(self.store, self.policy, self.log)
        # Phase 13D: durable single-use execution ledger (replay protection
        # across restarts); DATA only - it never authorizes or decides.
        self.executor = Executor(
            self.tools, self.store, self.log, ledger=make_execution_ledger(cfg.persistence)
        )
        self.selector = ModelSelector(cfg.routing)
        runtime_workspace = Path(workspace_root or cfg.persistence.task_workspace)
        # Phase 8: production verifiers are the DEFAULT, injected through the
        # same DI fields so tests/hosts may replace the boundary (SPEC §8.25).
        from nomadicos.verification.goal import PredicateGoalVerifier
        from nomadicos.verification.step import PredicateStepVerifier

        self.runtime = TaskRuntime(
            config=cfg,
            logger=self.log,
            store=self.store,
            authz=self.authz,
            validator=self.validator,
            tools=self.tools,
            executor=self.executor,
            registry=self.registry,
            selector=self.selector,
            escalator=EscalationPolicy(self.selector, cfg.budget.max_escalations),
            planner=planner or StructuralPlanner(),
            engines=engines,
            workspace_root=runtime_workspace,
            default_engine=cfg.inference.default_engine,
            step_verifier=PredicateStepVerifier(runtime_workspace),
            goal_verifier=PredicateGoalVerifier(runtime_workspace),
            full_pc=self.store.has_full_autonomy,
        )
        self.memory_runtime = build_runtime_memory(
            Path(state_dir or cfg.persistence.state_dir), cfg.memory, logger=self.log
        )
        self.runtime.memory = self.memory_runtime
        # Phase 13B: durable graph checkpoints behind the existing seam.
        # Orchestration state is DATA: restore re-enters the normal
        # validate -> authorize -> execute -> verify pipeline; nothing here
        # creates SUCCESS or authority (SPEC §34, §47).
        self.checkpointer = make_durable_saver(cfg.persistence)
        self.graph = build_graph(self.runtime, checkpointer=self.checkpointer)
        # Phase 13C: durable task truth (SPEC §34) separate from the
        # orchestration checkpoint; status stored as DATA only.
        self.task_store = make_task_store(cfg.persistence)
        self._run_dir = Path(state_dir or cfg.persistence.state_dir) / "runs"

    # ------------------------------------------------------- restart ------
    def _record_durable_task(self, summary: RunSummary, final: dict[str, Any]) -> None:
        """Upsert durable task truth (SPEC §34, §35 auditability, Phase 13C/G).

        TaskRecord.status is DATA copied from the task state the graph
        produced - persistence never generates SUCCESS. Best-effort: a
        durable-store failure never changes the task outcome. The record
        carries the §35 audit evidence: proposals (references), issued
        authorized actions (non-executable refs), executions,
        verifications, and the correlated event log."""
        try:
            existing = self.task_store.load_task(summary.task_id)
            if existing is None:
                record = new_record(
                    summary.task_id, str(final.get("goal_text", summary.task_id))[:2000]
                )
            else:
                record = existing
            task_id = summary.task_id
            proposals = list(final.get("proposals") or [])[-100:]
            executed_fps = {
                str(e.action_fingerprint) for e in (final.get("executions") or [])
            }
            action_refs = []
            for proposal in proposals:
                action_refs.append(
                    AuthorizedActionRef(
                        action_id="",
                        proposal_id=str(proposal.id),
                        fingerprint=str(proposal.fingerprint())[:64],
                        capability=f"{proposal.tool}.{proposal.operation}",
                        authority_epoch=0,
                        # lifecycle label from the state alone (not an
                        # authority statement): the proposal was executed
                        # under a real grant, or is still only proposed
                        granted_by=(
                            "authorized"
                            if str(proposal.fingerprint()) in executed_fps
                            else "proposed"
                        ),
                    )
                )
            audit_events = []
            for event in self.log.events(task_id)[-500:]:
                raw = getattr(event, "to_dict", None)
                payload = dict(raw()) if callable(raw) else {"raw": str(event)[:200]}
                if not payload.get("timestamp"):
                    payload["timestamp"] = now_iso()
                audit_events.append(payload)
            update = record.model_copy(
                update={
                    "status": summary.status,
                    "outcome_note": (summary.outcome_note or "")[:400],
                    "executions": list(final.get("executions") or [])[-200:],
                    "verifications": list(final.get("verifications") or [])[-100:],
                    "authorized_actions": action_refs[-100:],
                    "audit_events": audit_events,
                    "created_at": existing.created_at if existing else record.created_at,
                    "updated_at": now_iso(),
                }
            )
            self.task_store.save_task(update)
        except PersistenceError:
            pass  # durable task truth is best-effort; the checkpoint remains

    def list_durable_tasks(self) -> list[dict[str, Any]]:
        """Durable task discovery: task-state records + persisted checkpoint
        threads. No second task registry - these ARE the durable stores."""
        tasks: dict[str, dict[str, Any]] = {}
        for record_id in self.task_store.task_ids(limit=200):
            try:
                record = self.task_store.load_task(record_id)
            except PersistenceError:
                record = None
            if record is not None:
                tasks[record.task_id] = {
                    "task_id": record.task_id,
                    "status": record.status.value,
                    "source": "task-state",
                }
        for thread in self.checkpointer.thread_ids():
            if not thread.startswith("nomadic:"):
                continue
            task_id = thread.split(":", 1)[-1]
            if task_id in tasks:
                continue
            values = self.graph.get_state(
                {"configurable": {"thread_id": thread}}
            ).values or {}
            status = values.get("task_status")
            if status is not None:
                tasks[task_id] = {
                    "task_id": task_id,
                    "status": str(status),
                    "source": "checkpoint",
                }
        return [tasks[key] for key in sorted(tasks)]

    async def resume_task(
        self, task_id: str, *, thread_prefix: str = "nomadic"
    ) -> RunSummary:
        """Safe restart lifecycle (SPEC §47): restore DATA, then continue
        through the normal validate -> authorize -> execute -> verify
        pipeline. Never replays a serialized action; terminal tasks are
        returned as completed DATA without re-invoking the graph."""
        config = {"configurable": {"thread_id": f"{thread_prefix}:{task_id}"}}
        values = dict(self.graph.get_state(config).values or {})
        record = None
        try:
            record = self.task_store.load_task(task_id)
        except PersistenceError:
            record = None
        terminal = {"SUCCESS", "FAILED", "BLOCKED", "PARTIAL"}
        if record is not None and record.status.value in terminal:
            return self._restored_summary(record)
        if values.get("task_status") is not None:
            summary = self._summarize(values)
            if summary.status.value in ("SUCCESS", "FAILED", "BLOCKED", "PARTIAL"):
                self._persist(summary)
                return summary
            if summary.status is TaskStatus.WAITING_OWNER:
                return summary  # owner decision pending; never auto-answered
        if not values and record is None:
            raise PersistenceCorrupt(f"no durable task {task_id!r} to resume")
        if not values:
            if record is None:
                raise PersistenceCorrupt(f"no durable task {task_id!r} to resume")
            return self._restored_summary(record)
        self.log.log(EventType.RECOVERY_STARTED, task_id=task_id, result="RESUME")
        final = await self.graph.ainvoke(None, config)
        summary = self._summarize(final)
        self._memory_after_task(summary, final)
        self._record_durable_task(summary, final)
        self._persist(summary)
        return summary

    def _restored_summary(self, record: TaskRecord) -> RunSummary:
        """Completed task restored as DATA: historical outcome only, no
        re-execution, no new SUCCESS event."""
        return RunSummary(
            task_id=record.task_id,
            status=record.status,
            outcome_note=record.outcome_note or "restored completed task",
        )


    # ------------------------------------------------- memory accessors ---
    @property
    def memory_store(self) -> Any:
        """The durable memory store (DATA boundary) for hosts/tests."""
        return self.memory_runtime.store

    @property
    def working_memory(self) -> Any:
        return self.memory_runtime.working

    # ------------------------------------------------------- discovery ----
    async def sync_models(self) -> None:
        """Best-effort: register live engine models + models/ files. Missing
        engines remain unregistered; nothing is invented (SPEC §12)."""
        builder = RegistryBuilder(self.config.models)
        for name in ("llamacpp", "ollama"):
            engine = self.engines.get(name)
            if engine is None:
                continue
            try:
                await builder.sync_from_engine(self.registry, engine)
            except Exception:
                continue  # engine down: honest absence, not an error
        builder.build(self.registry, models_dir=self.config.inference.models_dir)

    # ------------------------------------------------------------ runs ----
    # ------------------------------------------------------------ runs ----
    async def run_goal(
        self,
        goal_text: str,
        *,
        task_id: str | None = None,
        constraints: list[str] | None = None,
        predicates: list[dict[str, Any]] | None = None,
        thread_prefix: str = "nomadic",
    ) -> RunSummary:
        goal = Goal(objective=goal_text, constraints=constraints or [])
        task = task_id or goal.id
        initial: dict[str, Any] = {
            "goal_text": goal_text,
            "goal_constraints": constraints or [],
            "goal_predicates": predicates or [],
            "task_id": task,
        }
        graph_config = {"configurable": {"thread_id": f"{thread_prefix}:{task}"}}
        memory_hooks.seed_working(self.memory_runtime, task, goal_text)
        final = await self.graph.ainvoke(initial, graph_config)
        summary = self._summarize(final)
        self._memory_after_task(summary, final)
        self._record_durable_task(summary, final)
        self._persist(summary)
        return summary

    async def resume_owner(
        self, task_id: str, decision: str, *, thread_prefix: str = "nomadic"
    ) -> RunSummary:
        """The host (owner) pathway only. The resume value goes to the
        authority resolver, never the other way around (SPEC §5, §7.12)."""
        graph_config = {"configurable": {"thread_id": f"{thread_prefix}:{task_id}"}}
        final = await self.graph.ainvoke(Command(resume=decision), graph_config)
        summary = self._summarize(final)
        self._record_durable_task(summary, final)
        self._persist(summary)
        return summary

    def status(self, task_id: str) -> dict[str, Any] | None:
        path = self._run_dir / f"{task_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def coding_report(self, task_id: str):
        """Rebuild a worker report from a finished task thread's state."""
        from nomadicos.agents.coding import build_worker_report

        final = self.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}}).values
        return build_worker_report(
            task_id=task_id,
            executions=list(final.get("executions") or []),
            state=dict(final),
            elapsed_s=0.0,
        )

    async def aclose(self) -> None:
        self.supervisor.kill_all()
        for engine in self.engines.values():
            await engine.aclose()

    # --------------------------------------------------------- coding ------
    async def run_coding(
        self,
        goal_text: str,
        *,
        repo: str | Path,
        test_command: str | None = None,
        require_files: list[str] | None = None,
        require_content: list[tuple[str, str]] | None = None,
        constraints: list[str] | None = None,
        task_id: str | None = None,
        ask_owner: Any = None,
        predicates: list[dict[str, Any]] | None = None,
        critic_model: str | None = None,
    ):
        """Coding task on the SAME task graph (SPEC §9.24).

        The only swaps are read-only strategies (planner/context) and
        workspace scoping to the owner-designated repo (§9.5). Test files
        discovered at planning time become owner-instruction conflicts, so
        an edit to them asks OWNER first (§9.14 test-integrity guard).
        """
        import time

        from nomadicos.agents.coding import CodingWorker, build_worker_report
        from nomadicos.agents.inspect import RepoInspector
        from nomadicos.agents.planning import CodingPlanner, predicates_for_coding_task
        from nomadicos.kernel.events import EventType
        from nomadicos.verification.goal import PredicateGoalVerifier
        from nomadicos.verification.step import PredicateStepVerifier

        repo_root = Path(repo).resolve()
        if not repo_root.is_dir():
            raise ValueError(f"coding repo not found: {repo_root}")
        inspector = RepoInspector(repo_root)
        focus = [str(x) for x in (require_files or [])] + goal_text.lower().split()[:4]
        survey = inspector.survey(focus_terms=focus)
        self.log.log(
            EventType.REPOSITORY_INSPECTED,
            task_id=task_id or "pending",
            result=f"{len(survey.tree)} files, git={'yes' if survey.has_git else 'no'}",
            payload={"tests": survey.test_files[:12]},
        )
        predicates_list = predicates_for_coding_task(
            test_command=test_command,
            require_files=require_files or [],
            require_content=require_content or [],
        )
        if predicates is not None:
            predicates_list = list(predicates)
        worker = CodingWorker(repo_root, focus_terms=focus)

        r = self.runtime
        saved = (
            r.planner,
            r.context_builder,
            r.workspace_root,
            r.workspace_per_task,
            r.step_verifier,
            r.goal_verifier,
            r.critic,
        )
        instruction_ids: list[str] = []
        try:
            r.planner = CodingPlanner(has_git=survey.has_git)
            r.context_builder = worker.context_builder()
            r.workspace_root = repo_root
            r.workspace_per_task = False
            r.step_verifier = PredicateStepVerifier(repo_root, per_task=False)
            r.goal_verifier = PredicateGoalVerifier(repo_root, per_task=False)
            if critic_model:
                from nomadicos.agents.critic import ModelCritic

                try:
                    base = self.registry.get(critic_model)
                    self.registry.register(
                        base.model_copy(update={"roles": ["critic", "reasoning"]})
                    )
                except Exception:
                    pass  # unknown id: ModelCritic reports "no critic model" honestly
                r.critic = ModelCritic(self.registry, self.selector, r.engine_for)
            for test_file in survey.test_files[:24]:
                ins = self.store.add_instruction(
                    f"test file - editing it requires owner approval: {test_file}", test_file
                )
                instruction_ids.append(ins.id)
            started = time.monotonic()
            summary = await self.run_goal(
                goal_text,
                task_id=task_id,
                constraints=constraints or [],
                predicates=predicates_list,
            )
            # conflicts must be answered WHILE the coding wiring is active
            while ask_owner is not None and summary.waiting_owner():
                answer = str(ask_owner(summary.conflict or {}))
                summary = await self.resume_owner(summary.task_id, answer)
            final = self.graph.get_state(
                {"configurable": {"thread_id": f"nomadic:{summary.task_id}"}}
            ).values
            report = build_worker_report(
                task_id=summary.task_id,
                executions=list(final.get("executions") or []),
                state=final,
                elapsed_s=time.monotonic() - started,
            )
            return summary, report
        finally:
            for ins_id in instruction_ids:
                self.store.remove_instruction(ins_id)
            (
                r.planner,
                r.context_builder,
                r.workspace_root,
                r.workspace_per_task,
                r.step_verifier,
                r.goal_verifier,
                r.critic,
            ) = saved

    # ------------------------------------------------------ memory seam ---
    def _memory_after_task(self, summary: RunSummary, final: dict[str, Any]) -> None:
        """End-of-task memory boundary (Phase 11G, DATA only): episodic
        write from REAL verification artifacts + working-memory refresh.
        A memory problem is isolated HERE: it can never change status,
        verification, routing, or authority — it is observable only via
        summary.memory_note (and the MEMORY_UPDATED event on success)."""
        memory = self.runtime.memory
        if memory is None:
            summary.memory_note = "memory: not wired"
            return
        try:
            summary.memory_note = memory_hooks.episodic_hook(memory, summary.task_id, final)
            memory_hooks.update_working_from_state(memory, summary.task_id, final)
        except Exception:  # noqa: BLE001 - memory boundary isolation
            summary.memory_note = "memory: hook failed"

    # ---------------------------------------------------------- summary ---
    def _summarize(self, state: dict[str, Any]) -> RunSummary:
        executions = list(state.get("executions") or [])
        conflict = state.get("pending_conflict")
        goal_verdict: str | None = None
        goal_why: list[str] = []
        goal_verifier: str | None = None
        for v in state.get("verifications") or []:
            if v.level is VerificationLevel.GOAL:
                goal_verdict = v.verdict.value
                goal_why = v.why(4)
                goal_verifier = v.verifier
        return RunSummary(
            task_id=str(state.get("task_id", "?")),
            status=TaskStatus(state.get("task_status", "FAILED")),
            outcome_note=str(state.get("outcome_note", "")),
            model_id=state.get("model_id"),
            steps_used=int(state.get("total_steps_used", 0)),
            executions=len(executions),
            recoveries=len(state.get("recovery_count") or []),
            conflict=conflict if isinstance(conflict, dict) else None,
            goal_verdict=goal_verdict,
            goal_why=goal_why,
            goal_verifier=goal_verifier,
            critic_iterations=[dict(i) for i in (state.get("critic_iterations") or [])][-6:],
            critic_feedback_present=state.get("critic_feedback") is not None,
            last_observations=[o.summary for o in (state.get("observations") or [])[-3:]],
            failures=[f.model_dump(mode="json") for f in (state.get("failures") or [])][-3:],
        )

    def _persist(self, summary: RunSummary) -> None:
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            payload = summary.__dict__.copy()
            payload["status"] = summary.status.value
            (self._run_dir / f"{summary.task_id}.json").write_text(
                json.dumps(payload, default=str, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # status persistence must never break the task itself
