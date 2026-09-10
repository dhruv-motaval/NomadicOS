"""Agent roles for multi-agent orchestration (ADR-0030).

Same AgentRuntime loop, different identity + directive + budget per role.
Roles are policy, not code branches: the orchestrator composes them.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RoleSpec:
    name: str
    directive: str  # system-level instruction for this role's proposals
    max_steps: int = 3
    max_output_tokens: int = 512
    temperature: float = 0.2


PLANNER_ROLE = RoleSpec(
    name="planner",
    directive=(
        "You are the PLANNER agent. Decompose the goal into a short ordered list of "
        "concrete subtasks. Reply with ONE JSON object only: "
        '{"subtasks": [{"id": "s1", "description": "...", "task_type": "coding|research|'
        'reasoning|instruction_following|tool_use|long_context|general_generation", '
        '"depends_on": ["earlier subtask ids only"]}]} '
        "Rules: 2-6 subtasks max, dependencies may only reference EARLIER ids, "
        "each subtask must be independently executable by a worker agent using tools."
    ),
    max_steps=1,
    max_output_tokens=1024,
    temperature=0.1,
)

WORKER_ROLE = RoleSpec(
    name="worker",
    directive=(
        "You are a WORKER agent executing ONE subtask. Use the available tools to "
        "actually perform it, then reply with ONE JSON object only: "
        '{"tool": "<name>", "arguments": {...}, "finished": true|false} '
        "Set finished=true when this subtask's evidence is complete."
    ),
    max_steps=3,
    max_output_tokens=512,
    temperature=0.1,
)

SYNTHESIZER_ROLE = RoleSpec(
    name="synthesizer",
    directive=(
        "You are the SYNTHESIZER agent. Merge the worker results into one concise "
        "final answer for the owner. Report what was done and the verified outcome. "
        "Reply with plain text only."
    ),
    max_steps=1,
    max_output_tokens=1024,
    temperature=0.3,
)


__all__ = ["PLANNER_ROLE", "RoleSpec", "SYNTHESIZER_ROLE", "WORKER_ROLE"]
