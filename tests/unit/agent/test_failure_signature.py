"""STEP 6A.5 — normalized failure signature: equivalence rules + workspace
path derivation. Pure string logic; no model, no disk."""

from pathlib import Path

from nomadicos.agent.runtime import normalized_failure_signature
from nomadicos.constitution.policy_schema import RiskLevel
from nomadicos.core.task_ir import ActionKind, TaskAction


def _act(tool: str, arguments: dict) -> TaskAction:
    return TaskAction(
        kind=ActionKind.TOOL_CALL,
        tool=tool,
        arguments=arguments,
        risk=RiskLevel.LOW,
        capabilities=(("terminal.execute" if tool == "terminal" else "filesystem.read"),),
        task_id="t",
        step_id="s",
        attempt=1,
    )


def test_terminal_command_forms_share_one_signature() -> None:
    a = _act("terminal", {"command": "node test.js"})
    b = _act("terminal", {"command": "node", "args": ["test.js"]})
    c = _act("terminal", {"command": "NODE.EXE", "args": [" test.js "]})
    assert normalized_failure_signature(a) == normalized_failure_signature(b)
    assert normalized_failure_signature(a) == normalized_failure_signature(c)


def test_working_dir_forms_share_one_signature() -> None:
    ws = str(Path("C:/data/task-workspaces"))
    a = _act("terminal", {"command": "node", "args": ["t.js"], "working_dir": ws})
    b = _act("terminal", {"command": "node", "args": ["t.js"], "working_dir": "t"})
    # different working dirs DO differ when relative:
    assert normalized_failure_signature(a, ws) != normalized_failure_signature(b, ws)
    c = _act("terminal", {"command": "node", "args": ["t.js"], "working_dir": ws + "/t"})
    d = _act("terminal", {"command": "node", "args": ["t.js"], "working_dir": "t"})
    assert normalized_failure_signature(c, ws) == normalized_failure_signature(d, ws)


def test_distinct_commands_never_collide() -> None:
    a = _act("terminal", {"command": "node test.js"})
    b = _act("terminal", {"command": "node prod.js"})
    c = _act("terminal", {"command": "npm test.js"})
    d = _act("terminal", {"command": "node", "args": ["test.js", "--watch"]})
    sigs = {normalized_failure_signature(x) for x in (a, b, c, d)}
    assert len(sigs) == 4  # ordering/arg-count differences preserved


def test_filesystem_read_paths_equivalent_writes_differ() -> None:
    ws = "data\\task-workspaces"
    r1 = _act("filesystem", {"action": "read", "path": "sub/x.txt"})
    r2 = _act("filesystem", {"action": "read", "path": "data/task-workspaces/sub/x.txt"})
    assert normalized_failure_signature(r1, ws) == normalized_failure_signature(r2, ws)
    w1 = _act("filesystem", {"action": "write", "path": "a.txt", "content": "X"})
    w2 = _act("filesystem", {"action": "write", "path": "a.txt", "content": "Y"})
    assert normalized_failure_signature(w1, ws) != normalized_failure_signature(w2, ws)


def test_other_tools_fall_back_to_exact_label() -> None:
    a = _act("web.fetch", {"url": "https://x.test/a"})
    assert normalized_failure_signature(a) == f"other|{a.label()}"
