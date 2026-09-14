"""AgentRuntime: the canonical execution loop (BP Â§185, Â§50; Milestone Â§78).

    while not task.finished:
        observe â†’ plan/decide (model proposes structured tool call)
        â†’ security.authorize â†’ tools.execute â†’ observe evidence
        â†’ verify â†’ record step â†’ recover_or_finish

Budgets (BP Â§72) are enforced here, outside the model (I10). Every action is
mediated by the Security Gate (I5). Reports distinguish requested/done/
verified/failed/uncertainty (BP Â§180-181).
"""

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nomadicos.agent.executor import ExecutionResult
from nomadicos.agent.selector import ModelSelector
from nomadicos.agent.skills import SkillStore
from nomadicos.audit.base import (
    AuditEvent,
    AuditEventCategory,
    AuditSink,
)
from nomadicos.core.errors import (
    BudgetExceeded,
    NomadicError,
    PermissionDenied,
    SecurityPolicyViolation,
    StatePersistenceError,
    TaskTimeout,
    ToolExecutionError,
    ValidationError,
    VerificationFailed,
)
from nomadicos.core.events import EventBus, TraceContext
from nomadicos.core.lifecycle import (
    TASK_TRANSITIONS,
    TERMINAL_STATUSES,
    StateTransitionError,
    TaskState,
    TaskStatus,
)
from nomadicos.core.logging import get_logger
from nomadicos.core.task_ir import ActionClaim, ActionKind, TaskAction
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.experience.recorder import ExperienceRecorder, Outcome
from nomadicos.models.base import LocalModel
from nomadicos.security.budgets import TaskBudget, TaskBudgetTracker
from nomadicos.security.permissions import SubjectIdentity
from nomadicos.tools.base import ToolResult
from nomadicos.tools.gateway import ToolGateway

logger = get_logger("agent.runtime")

# Whole-message small-talk patterns (anchored): greetings, thanks, identity.
# A greeting embedded in a real request ("hi, open chrome") does NOT match.
_CONVERSATIONAL_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*hi+\s*(there|all|everyone|team|guys|friend)?\s*[!.,?]*\s*$",
        r"^\s*hey+\s*(there|all|everyone|team|guys|friend)?\s*[!.,?]*\s*$",
        r"^\s*hello+\s*(there|all|everyone|team|guys|friend)?\s*[!.,?]*\s*$",
        r"^\s*yo\s*[!.]*\s*$",
        r"^\s*sup\s*[!.?]*\s*$",
        r"^\s*namaste\s*[!.]*\s*$",
        r"^\s*good\s+(morning|afternoon|evening|day)\s*[!.]*\s*$",
        r"^\s*thanks?( you)?( a lot)?( so much)?\s*[!.]*\s*$",
        r"^\s*thank\s+you\s*[!.]*\s*$",
        r"^\s*(who|what)\s+are\s+you\s*[?.!]*\s*$",
        r"^\s*how\s+are\s+you\s*[?.!]*\s*$",
        r"^\s*what\s+can\s+you\s+do\s*[?.!]*\s*$",
        r"^\s*help\s*[!.?]*\s*$",
        r"^\s*(ok|okay|nice|cool|great|awesome|wow|lol|good|bad|sure|yes|no)\s*[!.?]*\s*$",
    )
)

# Informational questions / chat starters: "what is X", "tell me about Y".
# The action-verb veto below keeps question-shaped requests ("can you openâ€¦")
# in the task pipeline.
_QUESTION_STARTER = re.compile(
    r"^\s*(what|who|where|when|why|which|whose|how)\b.*$",
    re.IGNORECASE,
)
_CHAT_STARTER = re.compile(
    r"^\s*(tell me|explain|describe|define|do you know|give me)\b.*$",
    re.IGNORECASE,
)

# Explicit action verbs make ANY message a task, even question-shaped ones
# ("can you open chromeâ€¦"): the agent owns machine effects, not chit-chat.
_ACTION_VERB = re.compile(
    r"\b(open|run|execute|launch|start|stop|kill|write|create|delete|remove|"
    r"list|read|make|copy|move|rename|edit|install|download|upload|play|"
    r"search|find|close|print|restart|shutdown)\b",
    re.IGNORECASE,
)

# "How do I â€¦?" asks for instructions, never for action â€” even with verbs.
_HOW_TO_QUESTION = re.compile(
    r"^\s*how\s+(do|does|did|can|could|to|should|would)\b.*$",
    re.IGNORECASE,
)


class TaskReport(BaseModel):
    """BP Â§180: requested vs done vs verified vs failed vs uncertainty."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    goal: str
    status: TaskStatus
    requested: str
    completed: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    experience_id: str | None = None
    duration_seconds: float = 0.0
    model_latency_ms: float = 0.0  # total time spent generating across model calls
    reply: str | None = None  # conversational answer (no tool work needed)

    def render(self) -> str:
        if self.reply is not None:
            if self.status is TaskStatus.SUCCESS:
                base = self.reply
            else:
                base = f"[{self.status.value}] {self.reply}"
            tail = f"(time: {self.duration_seconds}s total, {self.model_latency_ms} ms model)"
            return f"{base}\n{tail}"
        completed_lines = [f"- {c}" for c in self.completed] or ["- nothing"]
        lines = [
            f"Task {self.task_id}: {self.status.value}",
            f"Requested: {self.requested}",
            "Completed:",
            *completed_lines,
        ]
        lines.append(f"Time: {self.duration_seconds}s total | model {self.model_latency_ms} ms")
        if self.verification:
            lines.append("Verification: " + "; ".join(self.verification))
        if self.failed:
            lines.append("Failed: " + "; ".join(self.failed))
        if self.uncertainty:
            lines.append("Uncertainty: " + "; ".join(self.uncertainty))
        return "\n".join(lines)


def _format_conversation(entries: list) -> str:
    """Human-readable conversation lines — 4B models quote plain paths far
    more reliably than JSON with escaped backslashes."""
    lines = []
    for e in entries:
        if not isinstance(e, dict):
            lines.append(str(e)[:200])
            continue
        did = "; ".join(e.get("completed", []) or []) or (e.get("reply") or "").strip()
        lines.append(
            f'- Owner asked: "{e.get("goal", "")}" -> status: {e.get("status", "")}'
            + (f" -> {did}" if did else "")
        )
    return "\n".join(lines)


class _LatencyProbe:
    """Accumulates wall-clock time spent inside model.generate calls (I10:
    measured, not assumed). Transparent proxy over the LocalModel."""

    def __init__(self) -> None:
        self.ms = 0.0

    def wrap(self, model: Any) -> Any:
        probe = self
        inner = model

        class _Probed:  # noqa: N801 â€” probe is private
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            async def generate(self, request: Any) -> Any:
                t0 = time.perf_counter()
                result = await inner.generate(request)
                probe.ms += (time.perf_counter() - t0) * 1000.0
                return result

        return _Probed()


class AgentRuntime:
    """Owns task execution. The model proposes structured tool calls; the
    Security Gate authorizes; the Tool Gateway executes; evidence verifies."""

    def __init__(
        self,
        *,
        selector: ModelSelector,
        manager: Any,  # ModelManager
        gateway: ToolGateway,
        audit_sink: AuditSink,
        recorder: ExperienceRecorder,
        evaluator: EvaluationEngine,
        bus: EventBus | None = None,
        budget: TaskBudget | None = None,
        memory: Any | None = None,  # MemoryEngine (BP Â§376-420)
        memory_context: list | None = None,  # pre-retrieved memories for this goal
        skills: SkillStore | None = None,  # machine-local learned skills
        machine_profile: str | None = None,  # environment facts for every task
        workspace_root: str | None = None,  # where task files must be written
        conversation: list | None = None,  # recent session exchanges (BP §376)
    ) -> None:
        self._selector = selector
        self._manager = manager
        self._gateway = gateway
        self._audit = audit_sink
        self._recorder = recorder
        self._evaluator = evaluator
        self._bus = bus
        self._budget_cfg = budget or TaskBudget()
        self._memory = memory
        self._memory_context = memory_context or []
        self._skills = skills
        self._machine_profile = machine_profile or ""
        self._workspace_root = workspace_root or ""
        self._conversation_log = conversation or []
        # Pipeline agents: model selection + handling are explicit agent steps
        # (BP Â§364) â€” constructed lazily since they wrap this runtime.
        from nomadicos.agent.pipeline_agents import ModelHandlerAgent, SelectorAgent

        self.selector_agent = SelectorAgent(self)
        self.handler_agent = ModelHandlerAgent(self)

    async def execute_task(
        self,
        goal: str,
        identity: SubjectIdentity,
        *,
        model_id: str | None = None,
        max_steps: int = 8,
        state_sink: ("Callable[[str, TaskStatus, TaskStatus], Awaitable[None]] | None") = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> TaskReport:
        """Run one task through the canonical loop (BP Â§185, Â§78)."""
        started = time.monotonic()
        budget = TaskBudgetTracker(self._budget_cfg)
        import uuid

        task_id = identity.task_id or str(uuid.uuid4())
        trace = TraceContext(
            request_id=identity.run_id or task_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            task_id=task_id,
        )

        model_latency = _LatencyProbe()
        completed: list[str] = []
        verification_notes: list[str] = []
        failed: list[str] = []
        evidence_bundles: list[tuple[str, Any]] = []
        reply: str | None = None
        # STEP 4: the ONE authoritative task lifecycle. Order is
        # validate legal -> persist CAS (durable) -> flip TaskState. A
        # persistence failure may NEVER leave an unrecorded state claimed.
        state = TaskState(task_id=task_id, status=TaskStatus.CREATED)

        async def _advance(to: TaskStatus, *, soft: bool = False) -> bool:
            current = state.status
            if current is to:
                return True
            if to not in TASK_TRANSITIONS[current]:
                raise StateTransitionError(f"illegal transition {current.value} -> {to.value}")
            if state_sink is not None:
                try:
                    await state_sink(task_id, current, to)
                except Exception as exc:  # noqa: BLE001 — durable-first rule
                    failed.append(
                        f"state persistence failed for {to.value}: {type(exc).__name__}: {exc}"
                    )
                    if soft:
                        return False
                    raise StatePersistenceError(
                        f"cannot continue without persisting state {to.value} (task {task_id})"
                    ) from exc
            state.transition(to)
            # lifecycle-owned audit: explicit state transitions are first-class
            # events (plan STEP 5 §8 TASK_STATE_TRANSITION)
            await self._audit.append(
                AuditEvent(
                    category=AuditEventCategory.TASK_EVENT,
                    user_id=trace.user_id,
                    session_id=trace.session_id,
                    task_id=task_id,
                    run_id=trace.run_id,
                    decision=f"STATE_TRANSITION:{current.value}>{to.value}",
                    subject=task_id,
                )
            )
            return True

        async def _terminal(to: TaskStatus) -> None:
            if not await _advance(to, soft=True):
                await _advance(TaskStatus.FAILED, soft=True)

        async def _hard(to: TaskStatus) -> TaskReport | None:
            """Mid-flight states are hard: if the authoritative DB cannot
            hold them, we stop before executing anything side-effectful.
            """
            try:
                await _advance(to)
                return None
            except StatePersistenceError:
                failed.append("task aborted: authoritative state not persisted")
                return TaskReport(
                    task_id=task_id,
                    goal=goal,
                    status=TaskStatus.FAILED,
                    requested=goal,
                    completed=list(completed),
                    verification=list(verification_notes),
                    failed=list(dict.fromkeys(failed)),
                    duration_seconds=round(time.monotonic() - started, 2),
                    model_latency_ms=round(model_latency.ms, 1),
                )

        async def _ensure_failed() -> None:
            if state.status in TERMINAL_STATUSES or state.status is TaskStatus.BLOCKED:
                return
            if (
                TaskStatus.FAILED not in TASK_TRANSITIONS[state.status]
                and TaskStatus.RUNNING in TASK_TRANSITIONS[state.status]
            ):
                await _advance(TaskStatus.RUNNING, soft=True)
            await _advance(TaskStatus.FAILED, soft=True)

        async def _ensure_cancelled() -> None:
            if state.status in TERMINAL_STATUSES:
                return
            if TaskStatus.CANCELLED not in TASK_TRANSITIONS[state.status]:
                if TaskStatus.RUNNING in TASK_TRANSITIONS[state.status]:
                    await _advance(TaskStatus.RUNNING, soft=True)
                else:
                    return
            await _advance(TaskStatus.CANCELLED, soft=True)

        # 1. Model selection + handling as agents (BP Â§97, Â§320, Â§364).
        abort = await _hard(TaskStatus.PLANNED)
        if abort is not None:
            return abort
        if model_id is None:
            decision = await self.selector_agent.select(goal)
            task_family = decision.task_family
            model_id = decision.model_id
            selection_reason = {**decision.reason, "task_family": task_family}
        else:
            task_family = "general"
            selection_reason = {"pinned": True}
        model = await self.handler_agent.ensure_model(model_id)
        model = model_latency.wrap(model)
        abort = await _hard(TaskStatus.AUTHORIZED)
        if abort is not None:
            return abort
        logger.info(
            "task started model=%s task_family=%s selection_reason=%s",
            model_id,
            task_family,
            selection_reason,
        )
        abort = await _hard(TaskStatus.RUNNING)
        if abort is not None:
            return abort

        await self._audit_task(trace, task_id, "TASK_START", model_id)
        # Failure evidence accumulates ACROSS attempts: an attempt-2 wipe of
        # `failed` erased the true root cause from the report (evidence rule).
        all_failures: list[str] = []

        async def _run_execution(attempt_no: int) -> None:
            """One full attempt: proposal loop with a FRESH budget. On attempt 2
            the skills learned from attempt 1 are injected by _propose â€” this is
            the self-improvement loop: fail â†’ learn â†’ retry â†’ succeed."""
            nonlocal completed, verification_notes, failed, evidence_bundles, reply
            blocked = False
            completed, verification_notes, failed, evidence_bundles = [], [], [], []
            reply = None
            if attempt_no > 1:
                # Retry semantics (plan STEP 6): FAILED -> RECOVERING ->
                # RUNNING. Never FAILED -> SUCCESS directly.
                await _advance(TaskStatus.RECOVERING, soft=True)
                state.attempts = state.attempts + 1
                await _advance(TaskStatus.RUNNING, soft=True)
            budget = TaskBudgetTracker(self._budget_cfg)
            attempt_trace = trace.child(step_id=f"attempt-{attempt_no}")
            run_trace = trace

            try:
                intent = self._is_conversational(goal)
                if intent is None:
                    # Ambiguous phrasing/language: let the model classify it.
                    budget.check_model_call()
                    intent = await self._classify_intent(model, goal)
                if intent:
                    # Chat, not a task: nothing effectful happens, so the Security
                    # Gate is not involved (I5 untouched — there is no action to
                    # mediate). The model answers directly, without tool proposals.
                    # When context exists (history/memory), synthesis quality
                    # matters more than speed — escalate to the reasoning tier;
                    # a 4B model parrots context instead of reading it.
                    chat_model = model
                    if getattr(self, "_conversation_log", None) or self._memory_context:
                        try:
                            decision = await self.selector_agent.select_for_role(
                                goal, "synthesizer"
                            )
                            if decision.model_id != model_id:
                                chat_model = model_latency.wrap(
                                    await self.handler_agent.ensure_model(decision.model_id)
                                )
                                logger.info(
                                    "chat escalated to %s for context synthesis",
                                    decision.model_id,
                                )
                        except Exception:  # noqa: BLE001 — escalation best effort
                            logger.debug("chat escalation skipped", exc_info=True)
                    budget.check_model_call()
                    reply = await self._chat_reply(
                        chat_model, goal, getattr(self, "_conversation_log", None)
                    )
                    await _terminal(TaskStatus.SUCCESS)
                    return
                # 2. Execution loop (BP 185) — with a working memory: the
                # model's own reasoning is carried across steps. Every model
                # message becomes a canonical ActionClaim -> TaskAction before
                # policy or the executor may see it (rebuild plan S3).
                reasoning_history: list[str] = []
                for step in range(1, max_steps + 1):
                    if stop_requested is not None and stop_requested():
                        await _ensure_cancelled()
                        return
                    budget.check_step()
                    step_label = f"attempt-{attempt_no}-step-{step}"
                    run_trace = attempt_trace.child(step_id=step_label)

                    # 2a. Model proposes; parser validates into the claim IR.
                    budget.check_model_call()
                    claim = await self._propose(model, goal, completed, reasoning_history)
                    if claim.reasoning:
                        reasoning_history.append(claim.reasoning)

                    if claim.kind is ActionKind.TOOL_CALL:
                        pass  # canonicalized + executed below
                    elif claim.kind is ActionKind.REPLY:
                        reply = claim.reply
                        break
                    elif claim.kind is ActionKind.INVALID:
                        # Malformed output NEVER reads as finished/success
                        # (rebuild plan 6): failed step, then retry.
                        failed.append(
                            "model produced an unparseable proposal"
                            + (f" ({claim.reason})" if claim.reason else "")
                        )
                        budget.check_retry()
                        continue
                    elif claim.kind is ActionKind.FINISH:
                        if completed:
                            break  # post-loop decides the truthful status
                        if step == 1:
                            # Empty "finished" claim challenged ONCE (366).
                            budget.check_model_call()
                            challenge = await self._propose(
                                model,
                                f"{goal}\n\n(The goal above has NOT been started yet. "
                                "Do not declare finished. Propose the FIRST tool "
                                "call that moves toward the goal, or use a reply "
                                "if it needs no tool.)",
                                completed,
                                reasoning_history,
                            )
                            if challenge.reasoning:
                                reasoning_history.append(challenge.reasoning)
                            if challenge.kind is ActionKind.REPLY:
                                reply = challenge.reply
                                break
                            if challenge.kind is ActionKind.TOOL_CALL:
                                claim = challenge
                            else:
                                break  # nothing usable; fails truthfully
                        else:
                            break
                    else:  # pragma: no cover - defensive
                        break

                    # --- canonicalize: SYSTEM metadata, never model-supplied.
                    # Unknown/unrunnable action is denied at the registry here,
                    # so raw claims cannot reach policy or the executor.
                    tool_name = claim.tool or ""
                    try:
                        risk, capabilities = self._gateway.action_descriptor(
                            tool_name, claim.arguments
                        )
                        action = TaskAction.bind(
                            claim,
                            task_id=task_id,
                            step_id=step_label,
                            attempt=attempt_no,
                            risk=risk,
                            capabilities=capabilities,
                            origin_model=model_id,
                        )
                    except (PermissionDenied, ValidationError, ValueError) as exc:
                        failed.append(f"{tool_name or 'invalid claim'}: {exc}")
                        # registry-stage refusals are audited too (plan §11/18)
                        await self._gateway.audit_denial(
                            tool_name or "invalid",
                            str(exc),
                            identity,
                        )
                        budget.check_retry()
                        continue

                    identity_step = action.identity_for(identity)

                    # Loop guard: exact repeat of the previous executed step
                    # means the model is stuck (it opened Chrome 8 times once).
                    signature = action.label()
                    if completed and signature == completed[-1]:
                        break

                    gate_result = await self._mediated_execute(action, identity_step, budget)
                    if not gate_result.success:
                        failed.append(f"{tool_name}: {gate_result.error or 'unknown gate failure'}")
                        if "requires user confirmation" in (gate_result.error or ""):
                            # ASK cannot flip to ALLOW mid-task — there is no
                            # approver inside this execution. Stop without
                            # burning retries on an unchangeable refusal.
                            blocked = True
                            budget.spend_all_retries()
                            break
                        budget.check_retry()
                        continue

                    # 2c. Verification: evidence, not claims.
                    evidence_kind = self._gateway.get(tool_name).spec.evidence_kind
                    if evidence_kind in ("filesystem", "terminal"):
                        evidence = self._evidence_from(tool_name, gate_result)
                        if action.arguments.get("action"):
                            evidence.facts["action"] = action.arguments["action"]
                        try:
                            verdict = await self._evaluator.verify(evidence_kind, evidence)
                            evidence_bundles.append((evidence_kind, evidence))
                            logger.debug(
                                "verifier verdict checks=%s",
                                [(c.name, c.passed) for c in verdict.checks],
                            )
                            verification_notes.append(verdict.summary)
                            # VERIFICATION_RESULT event (plan STEP 5 §8): the
                            # verdict belongs to evidence assessment, NOT to the
                            # executor (which only reported success/failure).
                            await self._audit.append(
                                AuditEvent(
                                    category=AuditEventCategory.TASK_EVENT,
                                    user_id=run_trace.user_id,
                                    session_id=run_trace.session_id,
                                    task_id=task_id,
                                    run_id=run_trace.run_id,
                                    step_id=run_trace.step_id,
                                    subject=model_id,
                                    decision="VERIFICATION_RESULT",
                                    reason=verdict.summary[:1024],
                                    fields={
                                        "checks_passed": sum(1 for c in verdict.checks if c.passed),
                                        "checks_total": len(verdict.checks),
                                    },
                                )
                            )
                        except VerificationFailed as exc:
                            verification_notes.append(f"no verifier: {exc}")

                    completed.append(signature)
                    await self._audit_task(run_trace, task_id, "STEP_DONE", model_id)
                    if claim.finish:
                        # The model declared the goal reached AFTER this step —
                        # honor it: the loop ends here (honest, small-model fix).
                        break

                if blocked and not completed:
                    # Owner-decision waiting: durable BLOCKED, not failure.
                    await _advance(TaskStatus.BLOCKED, soft=True)
                elif completed:
                    await _terminal(
                        TaskStatus.PARTIALLY_COMPLETED if failed else TaskStatus.SUCCESS
                    )
                elif reply is not None:
                    # Conversational answer only: partial completion.
                    await _terminal(TaskStatus.PARTIALLY_COMPLETED)
                else:
                    failed.append("model declared the goal finished without executing any steps")
                    await _terminal(TaskStatus.FAILED)
                # (a plain-language explanation is added post-mortem below)
            except (BudgetExceeded, TaskTimeout, NomadicError) as exc:
                failed.append(str(exc))
                await _ensure_failed()
            except Exception as exc:  # noqa: BLE001 â€” surfaced in the truthful report
                failed.append(f"unexpected {type(exc).__name__}: {exc}")
                await _ensure_failed()

        # Self-improvement loop (max 2 attempts): attempt 1 runs; if it fails
        # with zero executed work, the failure is distilled into a skill note
        # and attempt 2 re-runs IMMEDIATELY with that knowledge injected
        # (fresh budget, same model â€” learning, not luck; BP Â§67, Â§147).
        max_attempts = 2
        for attempt_no in range(1, max_attempts + 1):
            await _run_execution(attempt_no)
            all_failures.extend(failed)
            if state.status is TaskStatus.CANCELLED:
                break
            if state.status not in TERMINAL_STATUSES and state.status is not TaskStatus.BLOCKED:
                failed.append("attempt ended without a terminal state")
                await _ensure_failed()
            if state.status is not TaskStatus.FAILED:
                break
            if completed:  # real work happened; a retry would duplicate it
                break
            if attempt_no < max_attempts:
                logger.info(
                    "attempt %d failed (%s) — learning and retrying with fresh knowledge",
                    attempt_no,
                    (failed[0][:120] if failed else "unknown"),
                )
                try:
                    await self._learn_skill(
                        model, goal, failed, task_failed=(state.status is TaskStatus.FAILED)
                    )
                except Exception:  # noqa: BLE001 — learning is best effort
                    logger.debug("skill learning failed", exc_info=True)
                # Escalation (owner spec): attempt 2 switches to the strongest
                # tool-capable model available — more thinking when needed.
                try:
                    strongest = await self.selector_agent.strongest(tool_use=True)
                    if strongest != model_id:
                        model = await self.handler_agent.ensure_model(strongest)
                        model = model_latency.wrap(model)
                        model_id = strongest
                        logger.info("escalated to strongest model: %s", strongest)
                except Exception:  # noqa: BLE001 — escalation is best effort
                    logger.debug("escalation skipped", exc_info=True)

        # Post-mortem: on total failure with zero executed steps, fetch a
        # plain-language explanation for the user. Best effort and truthful â€”
        # the FAILED status is never softened (BP Â§366).
        if state.status is TaskStatus.FAILED and not completed and reply is None and all_failures:
            reply = await self._explain_failure(
                model, goal, failed[0] if failed else all_failures[0]
            )

        # Self-implementation (owner vision: the toolbox grows): a successful
        # task that wrote+ran a script may deserve a permanent generated tool.
        # One bounded model call, best effort, all local (I11) — the script is
        # saved under data/scripts/ where the owner can read or delete it (I4).
        if state.status is TaskStatus.SUCCESS and any("filesystem" in c for c in completed):
            try:
                await self._learn_tool(model, goal, completed)
            except Exception:  # noqa: BLE001 — tool learning is best effort
                logger.debug("tool learning failed", exc_info=True)

        # 3. Evaluation (BP §100) + Experience (BP §95, §18) on every exit path.
        duration = time.monotonic() - started
        record = await self._evaluator.evaluate_run(
            task_id=task_id,
            run_id=identity.run_id or task_id,
            evidence_bundles=evidence_bundles,
            steps_taken=len(completed),
            retries_used=int(budget.snapshot()["retries"]),
            duration_seconds=duration,
        )
        outcome = (
            Outcome.SUCCESS
            if state.status is TaskStatus.SUCCESS
            else Outcome.PARTIAL
            if state.status is TaskStatus.PARTIALLY_COMPLETED
            else Outcome.FAILURE
        )
        experience = await self._recorder.finish(
            task_id=task_id,
            session_id=identity.session_id,
            outcome=outcome,
            summary=f"{goal[:200]} -> {record.verdict_summary[:180]}",
            model_id=model_id,
            tool_calls=int(budget.snapshot()["tool_calls"]),
            steps=len(completed),
            verified=record.verified,
            evidence={"score": record.score},
        )
        await self._audit_task(trace, task_id, "TASK_END", model_id)

        report = TaskReport(
            task_id=task_id,
            goal=goal,
            status=state.status,
            requested=goal,
            completed=completed,
            verification=verification_notes,
            failed=list(dict.fromkeys(all_failures)),  # full evidence
            experience_id=str(experience.experience_id),
            duration_seconds=round(duration, 2),
            model_latency_ms=round(model_latency.ms, 1),
            reply=reply,
        )
        logger.info(
            "task finished task=%s status=%s verified=%s",
            task_id,
            state.status.value,
            record.verified,
        )
        return report

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _task_family(goal: str) -> str:
        """Route the goal to a selection family so the right class of model
        serves it (BP Â§320). Cheap keyword routing; the selector still scores
        candidates within the family. Action verbs â†’ automation (needs reliable
        tool-argument generation, i.e. the biggest brain available)."""
        text = goal.lower()
        if _ACTION_VERB.search(text) or any(
            w in text for w in ("code", "script", "function", "program", "debug", "refactor")
        ):
            return "automation"
        if any(w in text for w in ("why", "reason", "explain", "compare", "analyze", "plan")):
            return "reasoning"
        return "general"

    @staticmethod
    def _is_conversational(goal: str) -> bool | None:
        """Informational/chat detection: greetings, questions, explain-starters.

        Any explicit action verb ('open chrome', 'list files') forces the task
        pipeline even if the message is question-shaped. Small models cannot
        be trusted to follow reply-vs-tool prompt rules reliably, so this
        classification is deterministic here in the runtime.

        Returns None when the message is ambiguous (any language/phrasing) â€”
        the caller then asks the model to classify it."""
        text = goal.strip()
        if not text:
            return False
        if _HOW_TO_QUESTION.match(text):
            return True  # asks for instructions, not for the action itself
        if _ACTION_VERB.search(text):
            return False
        if text.endswith("?"):
            return True  # question without any action verb ("local what?")
        if any(p.match(text) for p in _CONVERSATIONAL_PATTERNS):
            return True
        if _QUESTION_STARTER.match(text) is not None:
            return True
        if _CHAT_STARTER.match(text) is not None:
            return True
        return None  # ambiguous (any language/phrasing) â€” let the model decide

    async def _classify_intent(self, model: LocalModel, goal: str) -> bool:
        """Model-driven chat/task classification for ambiguous messages.

        Fail-CLOSED to the enforced task path (forensic fix): an earlier bug
        (`!= "task"`) treated ANY non-"task" model output — empty, punctuation,
        confusion, injected JSON — as chat, so imperatives never reached the
        executor and the runtime leaked the model's raw text as a SUCCESS reply
        (violates BP §6: malformed model output must never mean "success").
        Now only a positive "chat" answer routes to chat; everything else runs
        the loop, where the Security Gate (not the model) is the final authority.
        """
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "Classify the user message. Reply with ONE word:\n"
            "- task: the user wants the assistant to PERFORM/CHANGE something "
            "on this machine (run, write, create, open, install, fix...)\n"
            "- chat: a question, greeting, or conversation\n"
            f"Message: {goal}"
        )
        try:
            result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=8))
            first = result.text.strip().lower().split()
            return bool(first) and first[0].startswith("chat")
        except Exception:  # noqa: BLE001 — classification failure ⇒ task (fail closed)
            return False

    async def _chat_reply(
        self, model: LocalModel, goal: str, conversation: list | None = None
    ) -> str:
        """Direct conversational answer — no tools, no gate (nothing effectful).

        Carries recent session context so follow-ups ('in which you wrote...')
        resolve against what actually happened."""
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "You are NomadicOS, a local-first AI assistant. Reply briefly and "
            "conversationally. Do not use tools. Do not output JSON.\n"
        )
        if conversation:
            prompt += (
                "Recent conversation with the owner (this is your memory - use "
                "the FACTS in it to answer; if the owner asks where a file is "
                "and a path appears below, state the exact full path; do not "
                "invent paths):\n" + _format_conversation(conversation) + "\n"
            )
        prompt += f"Message: {goal}"
        result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=256))
        return result.text.strip() or "â€¦"

    async def _explain_failure(self, model: LocalModel, goal: str, failure: str) -> str | None:
        """Plain-language explanation when the goal could not be executed.

        Best effort: any error here leaves ``reply`` unset (truthful report
        only, no invented explanation â€” BP Â§366)."""
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "You are NomadicOS, a local-first AI assistant. A task just failed. "
            "In ONE short sentence, restate the failure for the user. Copy the "
            "FACTS from the Failure line below - change wording only if needed. "
            "Do NOT add reasons, guesses, advice, remedies, or capability claims "
            "of your own. Do not output JSON.\n"
            f"Failure: {failure}\n"
            f"Goal: {goal}"
        )
        try:
            result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=128))
            text = result.text.strip()
            return text or None
        except Exception:  # noqa: BLE001 â€” explanation is best effort
            return None

    async def _learn_skill(
        self, model: LocalModel, goal: str, failed: list[str], *, task_failed: bool = False
    ) -> None:
        """Distill a failure into a telegraphic skill note (caveman/ponytail style).

        The local model reads the local failure and writes the minimal facts
        that make the next attempt succeed. Everything stays on this machine
        (I11). Skipped entirely when no SkillStore is configured.

        Anti-fabrication guard (BP §366): a FAILED task cannot produce success
        transcripts — notes claiming success are rejected, never stored."""
        if self._skills is None:
            return
        from nomadicos.models.base import GenerateRequest

        failed_context = (
            "IMPORTANT: the task FAILED. Write only NEXT-ATTEMPT instructions "
            "derived from the failures. Do NOT write any transcript of commands "
            "that were run, and NEVER claim anything succeeded.\n"
            if task_failed
            else ""
        )
        prompt = (
            "A task on this Windows machine needs a skill note so the next "
            "attempt succeeds. CAVEMAN style: max 8 short lines, only concrete "
            "facts - exact commands, exact paths, what failed on THIS machine. "
            "No explanation, no prose, no markdown headers. "
            + failed_context
            + "If nothing useful can be learned, output only: SKIP\n"
            f"Goal: {goal}\n"
            f"Failures: {json.dumps(failed[:3])}"
        )
        result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=256))
        text = result.text.strip()
        if not text or "SKIP" in text.upper()[:20]:
            return
        if task_failed:
            fabricated = re.search(
                r"created successfully|tested and runs|compiled successfully|"
                r"runs gui|worked|success",
                text,
                flags=re.IGNORECASE,
            )
            if fabricated:
                logger.warning(
                    "rejected fabricated skill note (failure task, success claim): %s",
                    fabricated.group(0),
                )
                return
        self._skills.save(goal, text)

    async def _learn_tool(self, model: LocalModel, goal: str, completed: list[str]) -> None:
        """Persist a reusable script born from this task (self-implementation).

        One bounded model call: if the completed steps show a script was
        written+run and it is REUSABLE, output it with the nomadicos-tool
        header. Saved under data/scripts/ (local, owner-inspectable, gated).
        Best effort: any error or SKIP means no tool is created."""
        if self._skills is None:
            return
        from nomadicos.models.base import GenerateRequest
        from nomadicos.tools.generated import parse_script_header, save_generated_script

        prompt = (
            "A task just succeeded on this Windows machine. If the completed "
            "steps included a script/automation that is REUSABLE for similar "
            "future goals, output that script so it becomes a permanent tool. "
            "Format: first line exactly '# nomadicos-tool', second line "
            "'# description: <one line>', third line "
            "'# arguments_schema: <json schema, may be {}>', then the Python "
            "code. The script must print ONE JSON line: "
            '{"summary": "...", "data": {...}}. '
            "Max 150 lines. Windows/PowerShell environment. If nothing here is "
            "reusable, output only: SKIP\n"
            f"Goal: {goal}\n"
            f"Completed steps: {json.dumps(completed[-6:])}"
        )
        result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=1600))
        text = result.text.strip()
        if not text or text.upper().startswith("SKIP"):
            return
        # unwrap markdown fences if present
        if text.startswith("```"):
            text = text.strip("`")
            if text.lstrip().startswith("python"):
                text = text.lstrip()[6:]
        path = save_generated_script(self._skills.root, goal, text)  # noqa: SLF001
        if path and parse_script_header(path) is not None:
            logger.info("learned new tool script: %s", path.name)

    async def _propose(
        self,
        model: LocalModel,
        goal: str,
        completed: list[str],
        reasoning_history: list[str] | None = None,
    ) -> ActionClaim:
        """Model proposes a structured tool call (BP §86) — with room to THINK.

        The model may reason before the JSON ('REASON:' lines or a native
        think channel). That reasoning is captured and carried into the next
        proposal: the loop gets a working memory instead of memoryless
        JSON blank-filling. Invalid proposals are rejected, never executed
        raw (BP §86, §142); schemas are surfaced for compliance (BP §141)."""
        from nomadicos.models.base import GenerateRequest

        tool_docs: list[dict[str, Any]] = []
        for name in self._gateway.registered_tools():
            spec = self._gateway.get(name).spec
            tool_docs.append(
                {
                    "tool": name,
                    "arguments": spec.arguments_schema.get("properties", {}),
                    "required": spec.arguments_schema.get("required", []),
                }
            )

        prompt = (
            "You are NomadicOS's task executor. Decide the next step for the goal. "
            "THINK FIRST: start your reply with 'REASON:' followed by 1-2 short "
            "sentences planning the step (skip only if truly obvious). Then output "
            "the decision as ONE JSON object, nothing after it:\n"
            '{"tool": "<tool name>", "arguments": {...}, "finished": false|true}\n'
            "Use finished=true ONLY when the goal is already accomplished by the "
            "completed steps listed — never before any step ran.\n"
            "If the goal needs no tool (pure chat/question), answer directly instead "
            "of calling a tool:\n"
            '{"reply": "<your answer>", "finished": true}\n'
            "CRITICAL: the terminal tool CAN launch apps (start chrome, start notepad). "
            "If a terminal command can achieve the goal, you MUST propose it - do not "
            "claim inability. Reply (finished=true, no tool) ONLY when no tool listed "
            "above can achieve the goal.\n"
            "FILE RULE: to create or modify files, ALWAYS use the filesystem tool "
            "(action: write) - never write file contents through terminal echo.\n"
            f"Goal: {goal}\n"
            f"Already completed steps: {completed[-3:]}\n"
        )
        if reasoning_history:
            prompt += (
                "\nYour reasoning so far (continue this line of thought, do not "
                f"repeat it): {json.dumps(reasoning_history[-2:])}\n"
            )
        if self._memory_context:
            memories = [
                {"content": m.content, "source": m.source, "verified": m.verified}
                for m in self._memory_context
            ]
            # BP §385/§352: memory is evidence to reason over, not authority —
            # and revalidation is the caller's discipline.
            prompt += (
                "\nRelevant past experience/memory (evidence, revalidate before "
                f"relying on it): {json.dumps(memories)}"
            )
        if self._skills is not None:
            notes = self._skills.find(goal)
            if notes:
                # Machine-local learned facts (I11: generated and stored locally).
                prompt += (
                    "\nMACHINE FACTS - these are verified commands that WORK on "
                    "this machine. Use them EXACTLY as written, word for word:\n"
                    + "\n".join(notes)
                    + "\nWhen a fact above covers the goal, propose EXACTLY that "
                    'command, in this JSON shape: {"tool": "terminal", '
                    '"arguments": {"command": "<the exact command from the fact>"}, '
                    '"finished": false}. Do NOT invent variations of it.'
                )
        if self._machine_profile:
            prompt += "\n" + self._machine_profile
        if self._workspace_root:
            prompt += (
                f"\nTASK WORKSPACE: {self._workspace_root} — ALL files you create "
                "or modify MUST be inside this exact directory. Any path outside "
                "it will be refused."
            )
        if self._conversation_log:
            prompt += (
                "\nRecent conversation with the owner (use this for follow-up "
                "references like 'that file'; if the owner asks where a file is "
                "and a path appears below, state the exact full path):\n"
                + _format_conversation(self._conversation_log)
            )
        result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=1024))
        raw = result.text

        # Capture native reasoning channels first (reasoning in the payload
        # itself wins); the THINK/REASON capture feeds loop working memory.
        reasoning: str | None = None
        think = re.search(r"<think>(.*?)</think>", raw, flags=re.DOTALL)
        if think:
            reasoning = think.group(1).strip()[:400]
        if not reasoning:
            reason_match = re.search(
                r"REASON:\s*(.+?)(?=\n\s*\{)", raw, flags=re.DOTALL | re.IGNORECASE
            )
            if reason_match:
                reasoning = reason_match.group(1).strip()[:400]

        claim = ActionClaim.from_model_text(raw)
        if reasoning and claim.reasoning is None and claim.kind is not ActionKind.INVALID:
            claim = claim.model_copy(update={"reasoning": reasoning[:400]})
        return claim

    async def _mediated_execute(
        self,
        action: object,
        identity: SubjectIdentity,
        budget: TaskBudgetTracker,
    ) -> ExecutionResult:
        """STEP 5 boundary: policy (gateway.authorize_action) decides; the
        TaskExecutor dispatches; THIS method only adapts outcomes into the
        structured ExecutionResult the loop consumes. It performs no policy,
        no parsing of model data, and no lifecycle mutation itself."""
        if not isinstance(action, TaskAction):
            raise ValidationError(
                f"executor requires canonical TaskAction, got {type(action).__name__}"
            )
        tool_name = action.tool or ""
        capability = action.capabilities[0] if action.capabilities else "none"

        def _no(
            capability_id: str,
            error: str,
            evidence: dict[str, Any],
            code: str = "NOT_AUTHORIZED",
        ) -> ExecutionResult:
            return ExecutionResult(
                success=False,
                action=tool_name,
                capability=capability_id,
                task_id=action.task_id,
                step_id=action.step_id,
                attempt=action.attempt,
                error=error,
                error_code=code,
                evidence=evidence,
            )

        try:
            _decision, prepared, refusal = await self._gateway.authorize_action(
                tool_name, action.arguments, identity, budget, task_ref=action
            )
        except ValidationError as exc:
            return _no(
                capability,
                f"invalid command: {exc}",
                {},
                code=str(getattr(exc, "context", {}).get("code") or "INVALID_ARGUMENTS"),
            )
        except (PermissionDenied, SecurityPolicyViolation) as exc:
            return _no(
                capability,
                f"security refusal: {exc}",
                {"decision": "REFUSED"},
                code=str(
                    getattr(exc, "context", {}).get("code")
                    or getattr(exc, "context", {}).get("reason_code")
                    or "PERMISSION_DENIED"
                ),
            )
        except ToolExecutionError as exc:
            # schema violation at the gateway (BP §142): failed step, not task
            return _no(
                capability,
                str(exc),
                {},
                code=str(getattr(exc, "context", {}).get("code") or "TOOL_EXECUTION_ERROR"),
            )
        if prepared is None:
            assert refusal is not None
            return _no(
                capability,
                refusal.error or "not authorized",
                dict(refusal.evidence),
                refusal.error_code or "NOT_AUTHORIZED",
            )
        return await self._gateway.executor.run(prepared)

    @staticmethod
    def _evidence_from(tool_name: str, result: "ToolResult | ExecutionResult") -> Any:
        from nomadicos.evaluation.base import Evidence

        facts = dict(result.evidence)
        if result.data and isinstance(result.data, dict):
            for key in ("exit_code", "stdout", "path", "exists", "action", "entries", "content"):
                if key in result.data:
                    facts[key] = result.data[key]
        return Evidence(kind="filesystem" if tool_name == "filesystem" else "terminal", facts=facts)

    async def _audit_task(
        self, trace: TraceContext, task_id: str, event: str, model_id: str | None
    ) -> None:
        await self._audit.append(
            AuditEvent(
                category=AuditEventCategory.TASK_EVENT,
                user_id=trace.user_id,
                session_id=trace.session_id,
                task_id=trace.task_id,
                run_id=trace.run_id,
                step_id=trace.step_id,
                subject=model_id,
                decision=event,
            )
        )


__all__ = ["AgentRuntime", "TaskReport"]
