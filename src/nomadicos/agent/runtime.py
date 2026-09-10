"""AgentRuntime: the canonical execution loop (BP §185, §50; Milestone §78).

    while not task.finished:
        observe → plan/decide (model proposes structured tool call)
        → security.authorize → tools.execute → observe evidence
        → verify → record step → recover_or_finish

Budgets (BP §72) are enforced here, outside the model (I10). Every action is
mediated by the Security Gate (I5). Reports distinguish requested/done/
verified/failed/uncertainty (BP §180-181).
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
# The action-verb veto below keeps question-shaped requests ("can you open…")
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
# ("can you open chrome…"): the agent owns machine effects, not chit-chat.
_ACTION_VERB = re.compile(
    r"\b(open|run|execute|launch|start|stop|kill|write|create|delete|remove|"
    r"list|read|make|copy|move|rename|edit|install|download|upload|play|"
    r"search|find|close|print|restart|shutdown)\b",
    re.IGNORECASE,
)

# "How do I …?" asks for instructions, never for action — even with verbs.
_HOW_TO_QUESTION = re.compile(
    r"^\s*how\s+(do|does|did|can|could|to|should|would)\b.*$",
    re.IGNORECASE,
)


class TaskReport(BaseModel):
    """BP §180: requested vs done vs verified vs failed vs uncertainty."""

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


class _LatencyProbe:
    """Accumulates wall-clock time spent inside model.generate calls (I10:
    measured, not assumed). Transparent proxy over the LocalModel."""

    def __init__(self) -> None:
        self.ms = 0.0

    def wrap(self, model: Any) -> Any:
        probe = self
        inner = model

        class _Probed:  # noqa: N801 — probe is private
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
        memory: Any | None = None,  # MemoryEngine (BP §376-420)
        memory_context: list | None = None,  # pre-retrieved memories for this goal
        skills: SkillStore | None = None,  # machine-local learned skills
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
        # Pipeline agents: model selection + handling are explicit agent steps
        # (BP §364) — constructed lazily since they wrap this runtime.
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
        """Run one task through the canonical loop (BP §185, §78)."""
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
        # 1. Model selection + handling as agents (BP §97, §320, §364).
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
                budget.check_model_call()
                reply = await self._chat_reply(model, goal)
                status = TaskStatus.SUCCESS
            else:
                # 2. Execution loop (BP §185).
                for step in range(1, max_steps + 1):
                    budget.check_step()
                    trace = trace.child(step_id=f"step-{step}")

                    # 2a. Model proposes a structured tool call (BP §86).
                    budget.check_model_call()
                    proposal = await self._propose(model, goal, completed)

                    # Proposal routing (unified):
                    # 1) tool present  → execute, REGARDLESS of the finished
                    #    flag (small models habitually mark their own reply
                    #    "finished"; the flag only ever breaks a no-tool reply).
                    # 2) reply present → conversational answer, break.
                    # 3) finished without any work (step 1, nothing completed)
                    #    → challenge ONCE, then fail truthfully (BP §366).
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

                    gate_result = await self._mediated_execute(
                        tool_name, arguments, identity, budget
                    )
                    if not gate_result.success:
                        failed.append(f"{tool_name}: {gate_result.error}")
                        if "requires user confirmation" in (gate_result.error or ""):
                            # ASK cannot flip to ALLOW mid-task — there is no
                            # approver inside this execution. Retrying the same
                            # refused proposal wastes budget (fail fast, BP §70).
                            budget.spend_all_retries()
                            break
                        budget.check_retry()
                        continue

                    # 2c. Verification (BP §28, §144: evidence, not claims).
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
                    await self._audit_task(trace, task_id, "STEP_DONE", model_id)

                if completed:
                    # Steps ran; failures make it partial (truthful report, BP §180).
                    status = TaskStatus.PARTIALLY_COMPLETED if failed else TaskStatus.SUCCESS
                elif reply is not None:
                    # Answered conversationally but nothing was materially done.
                    status = TaskStatus.PARTIALLY_COMPLETED
                else:
                    # The model claimed finished without executing anything —
                    # never a success (BP §366: claims are not evidence).
                    status = TaskStatus.FAILED
                    failed.append(
                        "model declared the goal finished without executing any steps"
                    )
                # (a plain-language explanation is added post-mortem below)
        except (BudgetExceeded, TaskTimeout, NomadicError) as exc:
            status = TaskStatus.FAILED
            failed.append(str(exc))
        except Exception as exc:  # noqa: BLE001 — surfaced in the truthful report
            status = TaskStatus.FAILED
            failed.append(f"unexpected {type(exc).__name__}: {exc}")

        # Post-mortem: on total failure with zero executed steps, fetch a
        # plain-language explanation for the user. Best effort and truthful —
        # the FAILED status is never softened (BP §366).
        if status is TaskStatus.FAILED and not completed and reply is None and failed:
            reply = await self._explain_failure(model, goal, failed[0])

        # Learning loop: a failed/partial task teaches the machine something
        # for next time — a telegraphic skill note saved locally (I11). Best
        # effort only; never blocks or alters the truthful report.
        if status in (TaskStatus.FAILED, TaskStatus.PARTIALLY_COMPLETED) and failed:
            try:
                await self._learn_skill(model, goal, failed)
            except Exception:  # noqa: BLE001 — learning is best effort
                logger.debug("skill learning failed", exc_info=True)

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
        serves it (BP §320). Cheap keyword routing; the selector still scores
        candidates within the family. Action verbs → automation (needs reliable
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

        Returns None when the message is ambiguous (any language/phrasing) —
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
        return None  # ambiguous (any language/phrasing) — let the model decide

    async def _classify_intent(self, model: LocalModel, goal: str) -> bool:
        """Model-driven chat/task classification for ambiguous messages.

        Understands any language the model knows (Hinglish included). Defaults
        to task on failure — preserving pre-classifier behavior (BP §185)."""
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
        except Exception:  # noqa: BLE001 — classification failure ⇒ task (old behavior)
            return False

    async def _chat_reply(self, model: LocalModel, goal: str) -> str:
        """Direct conversational answer — no tools, no gate (nothing effectful)."""
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "You are NomadicOS, a local-first AI assistant. Reply briefly and "
            "conversationally. Do not use tools. Do not output JSON.\n"
            f"Message: {goal}"
        )
        result = await model.generate(
            GenerateRequest(prompt=prompt, max_output_tokens=256)
        )
        return result.text.strip() or "…"

    async def _explain_failure(
        self, model: LocalModel, goal: str, failure: str
    ) -> str | None:
        """Plain-language explanation when the goal could not be executed.

        Best effort: any error here leaves ``reply`` unset (truthful report
        only, no invented explanation — BP §366)."""
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
        except Exception:  # noqa: BLE001 — explanation is best effort
            return None

    async def _learn_skill(self, model: LocalModel, goal: str, failed: list[str]) -> None:
        """Distill a failure into a telegraphic skill note (caveman/ponytail style).

        The local model reads the local failure and writes the minimal facts
        that make the next attempt succeed. Everything stays on this machine
        (I11). Skipped entirely when no SkillStore is configured."""
        if self._skills is None:
            return
        from nomadicos.models.base import GenerateRequest

        prompt = (
            "A task failed on this Windows machine. Write a skill note so the "
            "next attempt succeeds. CAVEMAN style: max 8 short lines, only "
            "concrete facts - exact commands, exact paths, what worked vs "
            "failed on THIS machine. No explanation, no prose, no markdown "
            "headers. If nothing useful can be learned, output only: SKIP\n"
            f"Goal: {goal}\n"
            f"Failures: {json.dumps(failed[:3])}"
        )
        result = await model.generate(
            GenerateRequest(prompt=prompt, max_output_tokens=256)
        )
        text = result.text.strip()
        if text and "SKIP" not in text.upper()[:20]:
            self._skills.save(goal, text)

    async def _propose(
        self, model: LocalModel, goal: str, completed: list[str]
    ) -> dict[str, Any]:
        """Model proposes a structured tool call (BP §86). Invalid proposals are
        rejected — never executed raw (BP §86, §142). Tool schemas are surfaced
        so the model can emit compliant arguments (BP §141)."""
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
            "Reply with ONE JSON object, nothing else:\n"
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
            f"Goal: {goal}\n"
            f"Already completed steps: {completed[-3:]}\n"
            "Available tools with argument schemas: "
            f"{json.dumps(tool_docs)}"
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
        result = await model.generate(GenerateRequest(prompt=prompt, max_output_tokens=1024))
        # Strip reasoning blocks — qwen/gpt-oss families may emit them around
        # the JSON; a leaked think-block previously broke extraction and the
        # proposal silently became "finished".
        text = re.sub(r"<think>.*?</think>", "", result.text, flags=re.DOTALL)
        text = re.sub(r"\[\[\[thinking.*?\]\]\]", "", text, flags=re.DOTALL | re.IGNORECASE)
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            proposal = json.loads(text[start:end])
            if not isinstance(proposal, dict):
                raise ValueError("proposal is not an object")
            return proposal
        except (ValueError, json.JSONDecodeError):
            # Malformed proposal ≠ finished. Signal malformed so the loop
            # counts it as a failed step and retries (truthful, BP §366).
            return {"malformed": True}

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
