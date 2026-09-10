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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    TaskTimeout,
    ToolExecutionError,
    VerificationFailed,
)
from nomadicos.core.events import EventBus, TraceContext
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.core.logging import get_logger
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
        lines.append(
            f"Time: {self.duration_seconds}s total | model {self.model_latency_ms} ms"
        )
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
        # 1. Model selection + handling as agents (BP Â§97, Â§320, Â§364).
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
        logger.info(
            "task started model=%s task_family=%s selection_reason=%s",
            model_id,
            task_family,
            selection_reason,
        )

        completed: list[str] = []
        verification_notes: list[str] = []
        failed: list[str] = []
        evidence_bundles: list[tuple[str, Any]] = []
        status = TaskStatus.RUNNING
        reply: str | None = None

        await self._audit_task(trace, task_id, "TASK_START", model_id)

        async def _run_execution(attempt_no: int) -> None:
            """One full attempt: proposal loop with a FRESH budget. On attempt 2
            the skills learned from attempt 1 are injected by _propose â€” this is
            the self-improvement loop: fail â†’ learn â†’ retry â†’ succeed."""
            nonlocal status, reply, model_latency
            nonlocal completed, failed, verification_notes, evidence_bundles
            completed, verification_notes = [], []
            failed, evidence_bundles = [], []
            reply = None
            status = TaskStatus.RUNNING
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
                                    await self.handler_agent.ensure_model(
                                        decision.model_id
                                    )
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
                    status = TaskStatus.SUCCESS
                    return
                # 2. Execution loop (BP §185) — with a working memory: the
                # model's own reasoning is carried across steps.
                reasoning_history: list[str] = []
                for step in range(1, max_steps + 1):
                    budget.check_step()
                    run_trace = attempt_trace.child(step_id=f"step-{step}")

                    # 2a. Model proposes a structured tool call (BP §86).
                    budget.check_model_call()
                    proposal = await self._propose(
                        model, goal, completed, reasoning_history
                    )
                    step_reasoning = proposal.pop("_reasoning", None)
                    if step_reasoning:
                        reasoning_history.append(step_reasoning)

                    # Proposal routing (unified):
                    # 1) tool present  â†’ execute, REGARDLESS of the finished
                    #    flag (small models habitually mark their own reply
                    #    "finished"; the flag only ever breaks a no-tool reply).
                    # 2) reply present â†’ conversational answer, break.
                    # 3) finished without any work (step 1, nothing completed)
                    #    â†’ challenge ONCE, then fail truthfully (BP Â§366).
                    tool_name = proposal.get("tool", "")
                    arguments = proposal.get("arguments", {}) or {}

                    if tool_name:
                        pass  # fall through to gate mediation below
                    elif proposal.get("reply"):
                        reply = proposal["reply"]
                        break
                    elif proposal.get("malformed"):
                        failed.append("model produced an unparseable proposal")
                        budget.check_retry()
                        continue
                    elif proposal.get("finished") and completed:
                        break  # goal claimed reached with work done; post-loop decides
                    else:
                        # finished/reply-less/no-tool proposal
                        if not completed and step == 1:
                            budget.check_model_call()
                            challenge = await self._propose(
                                model,
                                f"{goal}\n\n(The goal above has NOT been started yet. "
                                "Do not declare finished. Propose the FIRST tool "
                                "call that moves toward the goal, or use a reply "
                                "if it needs no tool.)",
                                completed,
                            )
                            c_tool = challenge.get("tool")
                            if c_tool:
                                tool_name = c_tool
                                arguments = challenge.get("arguments", {}) or {}
                            elif challenge.get("reply"):
                                reply = challenge["reply"]
                                break
                            else:
                                break  # nothing usable; fails truthfully post-loop
                        else:
                            break

                    if not tool_name:
                        break

                    # Loop guard: an exact repeat of the previous executed step
                    # means the model is stuck (it opened Chrome 8 times once).
                    # Treat the repeat as "done" instead of re-executing.
                    signature = f"{tool_name} {json.dumps(arguments)[:120]}"
                    if completed and signature == completed[-1]:
                        if proposal.get("finished"):
                            completed[-1] = signature
                        break

                    gate_result = await self._mediated_execute(
                        tool_name, arguments, identity, budget
                    )
                    if not gate_result.success:
                        failed.append(f"{tool_name}: {gate_result.error or 'unknown gate failure'}")
                        if "requires user confirmation" in (gate_result.error or ""):
                            # ASK cannot flip to ALLOW mid-task â€” there is no
                            # approver inside this execution. Retrying the same
                            # refused proposal wastes budget (fail fast, BP Â§70).
                            budget.spend_all_retries()
                            break
                        budget.check_retry()
                        continue

                    # 2c. Verification (BP Â§28, Â§144: evidence, not claims).
                    evidence_kind = self._gateway.get(tool_name).spec.evidence_kind
                    if evidence_kind in ("filesystem", "terminal"):
                        evidence = self._evidence_from(tool_name, gate_result)
                        if arguments.get("action"):
                            evidence.facts["action"] = arguments["action"]
                        try:
                            verdict = await self._evaluator.verify(evidence_kind, evidence)
                            evidence_bundles.append((evidence_kind, evidence))
                            logger.debug(
                                "verifier verdict checks=%s",
                                [(c.name, c.passed) for c in verdict.checks],
                            )
                            verification_notes.append(verdict.summary)
                        except VerificationFailed as exc:
                            verification_notes.append(f"no verifier: {exc}")

                    completed.append(f"{tool_name} {json.dumps(arguments)[:120]}")
                    await self._audit_task(run_trace, task_id, "STEP_DONE", model_id)
                    if proposal.get("finished"):
                        # The model declared the goal reached AFTER this step —
                        # honor it: the loop ends here (honest, small-model fix).
                        break

                if completed:
                    # Steps ran; failures make it partial (truthful report, BP Â§180).
                    status = TaskStatus.PARTIALLY_COMPLETED if failed else TaskStatus.SUCCESS
                elif reply is not None:
                    # Answered conversationally but nothing was materially done.
                    status = TaskStatus.PARTIALLY_COMPLETED
                else:
                    # The model claimed finished without executing anything â€”
                    # never a success (BP Â§366: claims are not evidence).
                    status = TaskStatus.FAILED
                    failed.append(
                        "model declared the goal finished without executing any steps"
                    )
                # (a plain-language explanation is added post-mortem below)
            except (BudgetExceeded, TaskTimeout, NomadicError) as exc:
                status = TaskStatus.FAILED
                failed.append(str(exc))
            except Exception as exc:  # noqa: BLE001 â€” surfaced in the truthful report
                status = TaskStatus.FAILED
                failed.append(f"unexpected {type(exc).__name__}: {exc}")

        # Self-improvement loop (max 2 attempts): attempt 1 runs; if it fails
        # with zero executed work, the failure is distilled into a skill note
        # and attempt 2 re-runs IMMEDIATELY with that knowledge injected
        # (fresh budget, same model â€” learning, not luck; BP Â§67, Â§147).
        max_attempts = 2
        for attempt_no in range(1, max_attempts + 1):
            await _run_execution(attempt_no)
            if status is not TaskStatus.FAILED:
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
                        model, goal, failed, task_failed=(status is TaskStatus.FAILED)
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
        if status is TaskStatus.FAILED and not completed and reply is None and failed:
            reply = await self._explain_failure(model, goal, failed[0])

        # Self-implementation (owner vision: the toolbox grows): a successful
        # task that wrote+ran a script may deserve a permanent generated tool.
        # One bounded model call, best effort, all local (I11) — the script is
        # saved under data/scripts/ where the owner can read or delete it (I4).
        if status is TaskStatus.SUCCESS and any("filesystem" in c for c in completed):
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
            if status is TaskStatus.SUCCESS
            else Outcome.PARTIAL
            if status is TaskStatus.PARTIALLY_COMPLETED
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
            status=status,
            requested=goal,
            completed=completed,
            verification=verification_notes,
            failed=list(dict.fromkeys(failed)),  # dedupe repeated refusals
            experience_id=str(experience.experience_id),
            duration_seconds=round(duration, 2),
            model_latency_ms=round(model_latency.ms, 1),
            reply=reply,
        )
        logger.info(
            "task finished task=%s status=%s verified=%s",
            task_id,
            status.value,
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
            w in text
            for w in ("code", "script", "function", "program", "debug", "refactor")
        ):
            return "automation"
        if any(
            w in text
            for w in ("why", "reason", "explain", "compare", "analyze", "plan")
        ):
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

        Understands any language the model knows (Hinglish included). Defaults
        to task on failure â€” preserving pre-classifier behavior (BP Â§185)."""
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "Classify the user message. Reply with ONE word:\n"
            "- task: the user wants the assistant to PERFORM/CHANGE something "
            "on this machine (run, write, create, open, install, fix...)\n"
            "- chat: a question, greeting, or conversation\n"
            f"Message: {goal}"
        )
        try:
            result = await model.generate(
                GenerateRequest(prompt=prompt, max_output_tokens=8)
            )
            return result.text.strip().lower() != "task"
        except Exception:  # noqa: BLE001 â€” classification failure â‡’ task (old behavior)
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
                "invent paths):\n"
                + _format_conversation(conversation)
                + "\n"
            )
        prompt += f"Message: {goal}"
        result = await model.generate(
            GenerateRequest(prompt=prompt, max_output_tokens=256)
        )
        return result.text.strip() or "â€¦"

    async def _explain_failure(
        self, model: LocalModel, goal: str, failure: str
    ) -> str | None:
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
            result = await model.generate(
                GenerateRequest(prompt=prompt, max_output_tokens=128)
            )
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
        result = await model.generate(
            GenerateRequest(prompt=prompt, max_output_tokens=256)
        )
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

    async def _learn_tool(
        self, model: LocalModel, goal: str, completed: list[str]
    ) -> None:
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
        result = await model.generate(
            GenerateRequest(prompt=prompt, max_output_tokens=1600)
        )
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
    ) -> dict[str, Any]:
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

        # Capture the model's reasoning: native think channels first, then the
        # REASON: preamble. This is the loop's working memory (BP §190: learned
        # reasoning is evidence, never authority).
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

        # Strip think blocks, then robustly extract the JSON object (reasoning
        # text may contain braces, so scan every '{' left to right).
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        text = re.sub(r"\[\[\[thinking.*?\]\]\]", "", text, flags=re.DOTALL | re.IGNORECASE)
        proposal = self._extract_json(text)
        if proposal is None:
            # Malformed proposal ≠ finished. Signal malformed so the loop
            # counts it as a failed step and retries (truthful, BP §366).
            proposal = {"malformed": True}
        if reasoning:
            proposal["_reasoning"] = reasoning
        return proposal

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Robust JSON object extraction: reasoning text may contain braces,
        so scan every '{' left to right and take the first parseable dict."""
        for start in (i for i, ch in enumerate(text) if ch == "{"):
            for end in range(len(text) - 1, start, -1):
                if text[end] != "}":
                    continue
                try:
                    parsed = json.loads(text[start : end + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    continue
                break
        return None

    async def _mediated_execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        identity: SubjectIdentity,
        budget: TaskBudgetTracker,
    ) -> ToolResult:
        try:
            return await self._gateway.execute(tool_name, arguments, identity, budget)
        except ToolExecutionError as exc:
            return ToolResult.failure(str(exc))
        except (PermissionDenied, SecurityPolicyViolation) as exc:
            return ToolResult.failure(
                f"security refusal: {exc}", evidence={"decision": "REFUSED"}
            )

    @staticmethod
    def _evidence_from(tool_name: str, result: ToolResult) -> Any:
        from nomadicos.evaluation.base import Evidence

        facts = dict(result.evidence)
        if result.data and isinstance(result.data, dict):
            for key in ("exit_code", "stdout", "path", "exists", "action", "entries", "content"):
                if key in result.data:
                    facts[key] = result.data[key]
        return Evidence(
            kind="filesystem" if tool_name == "filesystem" else "terminal", facts=facts
        )

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
                subject=model_id,
                decision=event,
            )
        )


__all__ = ["AgentRuntime", "TaskReport"]
