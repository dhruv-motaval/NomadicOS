"""Multi-agent missions (SPEC §46): one goal, several agents.

The parent agent only plans: NomadicBrain splits the goal into a task graph,
and every sub-task runs as its OWN agent — a complete, separate run of the
standard task graph with its own task id, model choice, retry budget, audit
trail and terminals. Independent agents run at the same time; an agent starts
once the sub-tasks it depends on have finished and sees their results.

Nothing here is a second execution path: each agent goes through the same
Action IR -> authorize -> execute -> verify pipeline as any goal. Agents share
the parent's workspace folder; files one agent is changing are leased to it
(``TaskRuntime.acquire_for``) and the desktop stays one-agent-at-a-time.
SUCCESS for the mission comes only from the goal verifier, evaluated over the
evidence of all its agents.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nomadicos.brain.planner.task_graph import TaskGraph
from nomadicos.brain.state import BrainState
from nomadicos.contracts.core import Goal, GoalPredicate, TaskStatus
from nomadicos.contracts.verification import VerificationOutcome
from nomadicos.inference.structured import EngineStructuredGenerator
from nomadicos.kernel.events import EventType
from nomadicos.orchestration.boundaries import NoGoalVerifier
from nomadicos.orchestration.brain_planner import BrainMissionPlanner
from nomadicos.persistence.contracts import validate_task_id
from nomadicos.router.analyzer import analyze_goal

if TYPE_CHECKING:
    from nomadicos.orchestration.app import NomadicApp, RunSummary

#: agent outcomes that let dependent agents start
_DONE_OK = frozenset({TaskStatus.SUCCESS.value, TaskStatus.PARTIAL.value})
#: outcomes worth one fresh retry (WAITING_OWNER / uncertain outcomes are not)
_RETRYABLE = frozenset({TaskStatus.FAILED.value, TaskStatus.BLOCKED.value})


@dataclass
class AgentRun:
    """One sub-task and the agent running it (data only)."""

    node_id: str
    task_id: str
    part: str
    depends_on: list[str]
    predicates: list[dict[str, Any]]
    status: str = "PENDING"
    note: str = ""
    attempts: int = 0
    summaries: list[RunSummary] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "agent": self.task_id,
            "part": self.part[:200],
            "depends_on": list(self.depends_on),
            "status": self.status,
            "attempts": self.attempts,
            "note": self.note[:300],
        }


def predicate_spec(gp: GoalPredicate) -> dict[str, Any]:
    """GoalPredicate -> the dict syntax ``parse_predicate`` reads."""
    if gp.predicate is not None:
        return {"type": gp.predicate.type, **gp.predicate.fields}
    assert gp.group is not None
    children = [predicate_spec(c) for c in gp.group.children]
    return {gp.group.op.value: children[0] if gp.group.op.value == "not" else children}


def _agent_id(parent: str, node_id: str) -> str:
    return validate_task_id(f"{parent[:96]}-{re.sub(r'[^A-Za-z0-9_-]', '_', node_id)[:30]}")


async def decompose(app: NomadicApp, goal: Goal) -> TaskGraph | None:
    """Ask the brain to split the goal; None when it cannot (one agent then
    does the whole goal). Model output is validated before it is used."""
    try:
        reqs = analyze_goal(goal, base_latency=app.config.routing.mode)
        model_id = app.selector.select(app.registry, reqs).selected.model_id
        planner = BrainMissionPlanner(
            generator=EngineStructuredGenerator(
                app.runtime.engine_for(model_id), model_id, temperature=0.0
            ),
            tool_registry=app.tools,
        )
        empty = BrainState(
            mission_id=goal.id, goal=goal, task_graph=TaskGraph.build(goal_id=goal.id)
        )
        decomposition = await planner.decompose_goal(empty)
        planner.plan_to_mission(decomposition, goal)  # deterministic validation
        graph: TaskGraph = decomposition.task_graph
        return graph
    except Exception as exc:  # noqa: BLE001 - a failed split is not a failed mission
        app.log.log(
            EventType.TASK_PLANNED,
            task_id=goal.id,
            result="SPLIT_FAILED",
            payload={"reason": str(getattr(exc, "message", exc))[:200]},
        )
        return None


def _child_goal(goal_text: str, agent: AgentRun, agents: dict[str, AgentRun]) -> str:
    lines = [
        agent.part,
        "",
        f"This is ONE part of a larger goal that several agents share: {goal_text!r}. "
        "Do only this part; other agents do the rest in the same folder.",
    ]
    if agent.depends_on:
        lines.append("Results of the parts this one builds on:")
        for dep in agent.depends_on:
            done = agents[dep]
            last = done.summaries[-1] if done.summaries else None
            seen = "; ".join(last.last_observations)[:300] if last else ""
            lines.append(f"- {done.part[:120]} -> {done.status}. {done.note[:200]} {seen}".rstrip())
    return "\n".join(lines)


async def run_mission_agents(
    app: NomadicApp,
    goal_text: str,
    *,
    constraints: list[str] | None = None,
    predicates: list[dict[str, Any]] | None = None,
    task_id: str | None = None,
    ask_owner: Callable[[dict[str, Any]], str] | None = None,
    max_concurrent: int | None = None,
) -> RunSummary:
    from nomadicos.orchestration.app import RunSummary

    budget = app.config.budget
    goal = Goal.from_spec(goal_text, constraints=constraints, predicates=predicates)
    parent = validate_task_id(task_id or goal.id)
    goal = goal.model_copy(update={"id": parent})

    graph = await decompose(app, goal)
    nodes = graph.topological_order() if graph is not None else []
    if len(nodes) < 2 or len(nodes) > budget.max_agents_per_mission:
        # nothing to split (or too many parts to run safely): one agent does it all
        return await app.run_goal(
            goal_text, task_id=parent, constraints=constraints, predicates=predicates
        )
    assert graph is not None

    agents: dict[str, AgentRun] = {}
    for nid in nodes:
        node = graph.node(nid)
        agents[nid] = AgentRun(
            node_id=nid,
            task_id=_agent_id(parent, nid),
            part=node.description,
            depends_on=list(node.depends_on),
            predicates=[predicate_spec(node.success_predicate)] if node.success_predicate else [],
        )
    app.log.log(EventType.TASK_CREATED, task_id=parent, result=goal_text[:120])
    app.log.log(
        EventType.TASK_PLANNED,
        task_id=parent,
        result=f"{len(agents)} agents",
        payload={"agents": [a.report() for a in agents.values()]},
    )

    slots = asyncio.Semaphore(max_concurrent or budget.max_concurrent_agents)
    finished = {nid: asyncio.Event() for nid in agents}
    owner_turn = asyncio.Lock()  # one owner question on screen at a time

    async def run_agent(agent: AgentRun, run_id: str, text: str) -> RunSummary:
        summary = await app.run_goal(
            text,
            task_id=run_id,
            constraints=constraints,
            predicates=agent.predicates,
            workspace_key=parent,
            brain_planner=False,  # already split: the agent works on its part directly
        )
        while ask_owner is not None and summary.waiting_owner():
            async with owner_turn:
                answer = await asyncio.to_thread(ask_owner, summary.conflict or {})
            summary = await app.resume_owner(run_id, answer)
        return summary

    async def drive(agent: AgentRun) -> None:
        try:
            for dep in agent.depends_on:
                await finished[dep].wait()
            blocked_by = [d for d in agent.depends_on if agents[d].status not in _DONE_OK]
            if blocked_by:
                agent.status = "SKIPPED"
                agent.note = "needs parts that did not finish: " + ", ".join(
                    agents[d].task_id for d in blocked_by
                )
                return
            async with slots:
                agent.status = "RUNNING"
                run_id = agent.task_id
                while True:
                    agent.attempts += 1
                    summary = await run_agent(agent, run_id, _child_goal(goal_text, agent, agents))
                    agent.summaries.append(summary)
                    agent.status, agent.note = summary.status.value, summary.outcome_note
                    values = app.graph.get_state(_thread(run_id)).values or {}
                    # one fresh retry for a clean failure; never after an
                    # uncertain side effect (SPEC §56.14)
                    if (
                        agent.attempts > 1
                        or agent.status not in _RETRYABLE
                        or values.get("outcome_uncertain")
                    ):
                        break
                    run_id = validate_task_id(f"{agent.task_id[:120]}-r2")
                    agent.part = (
                        f"{agent.part}\n(Previous attempt ended {agent.status}: {agent.note[:200]})"
                    )
        except Exception as exc:  # noqa: BLE001 - one agent's crash never stops its siblings
            agent.status = "ERROR"
            agent.note = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            finished[agent.node_id].set()

    await asyncio.gather(*(drive(a) for a in agents.values()))

    # the mission's verdict: the goal verifier over the evidence of ALL agents,
    # read in the shared folder (evidence is the parent's: its agents made it)
    executions = []
    for agent in agents.values():
        for summary in agent.summaries:
            values = app.graph.get_state(_thread(summary.task_id)).values or {}
            executions += [
                e.model_copy(update={"task_id": parent}) for e in values.get("executions") or []
            ]
    verifier = app.runtime.goal_verifier or NoGoalVerifier()
    result = verifier.verify_task(goal, executions)
    ok = all(a.status in _DONE_OK for a in agents.values())
    if result.verdict is VerificationOutcome.PASS and not isinstance(verifier, NoGoalVerifier):
        status, note = TaskStatus.SUCCESS, "goal verified: " + " | ".join(result.why(2))
    elif result.verdict is VerificationOutcome.BLOCKED:
        status, note = TaskStatus.BLOCKED, "goal verification blocked"
    elif result.verdict is VerificationOutcome.NOT_PASS:
        status, note = TaskStatus.FAILED, "goal verifier: " + ", ".join(result.missing)
    elif ok:
        status, note = TaskStatus.PARTIAL, "all agents finished; goal NOT independently verified"
    else:
        status, note = TaskStatus.FAILED, "some agents did not finish"
    waiting = [a.task_id for a in agents.values() if a.status == TaskStatus.WAITING_OWNER.value]
    if waiting and status is not TaskStatus.SUCCESS:
        status = TaskStatus.BLOCKED
        note = "waiting for the owner: " + ", ".join(waiting)
    app.log.log(EventType.GOAL_VERIFIED, task_id=parent, result=result.verdict.value)
    end_event = {
        TaskStatus.SUCCESS: EventType.TASK_COMPLETED,
        TaskStatus.BLOCKED: EventType.TASK_BLOCKED,
    }.get(status, EventType.TASK_FAILED)
    app.log.log(end_event, task_id=parent, result=status.value)

    summary = RunSummary(
        task_id=parent,
        status=status,
        outcome_note=note[:400],
        executions=len(executions),
        goal_verdict=result.verdict.value,
        goal_why=result.why(4),
        goal_verifier=result.verifier,
        agents=[a.report() for a in agents.values()],
    )
    app._record_durable_task(
        summary,
        {"goal_text": goal_text, "executions": executions, "verifications": [result]},
    )
    app._persist(summary)
    return summary


def _thread(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"nomadic:{task_id}"}}
