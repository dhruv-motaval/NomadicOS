"""Authority integration through the graph: deny, revoke-mid-run, phantom refs."""

from __future__ import annotations

from pathlib import Path

from helpers import make_app, write_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.execution import ExecutionStatus
from nomadicos.kernel.errors import AuthorizationDenied
from nomadicos.kernel.events import EventType
from nomadicos.tools.filesystem import FilesystemTool

GOAL = "Create file alpha.txt then beta.txt"
PRED = [
    {"type": "file_exists", "path": "alpha.txt"},
    {"type": "file_exists", "path": "beta.txt"},
]
SCRIPTS = [("alpha.txt", write_json("alpha.txt", "A")), ("beta.txt", write_json("beta.txt", "B"))]


async def test_no_owner_grant_blocks_execution_entirely(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=SCRIPTS, models=("models/small.gguf",), grant=False)
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert summary.executions == 0
    assert not list((tmp_path / "ws").rglob("*.txt"))
    assert any(e.type is EventType.AUTHORIZATION_DENIED for e in app.log.events(summary.task_id))


async def test_revoke_mid_run_prevents_further_executions(tmp_path: Path) -> None:
    app = make_app(tmp_path, scripts=SCRIPTS)
    store = app.store

    class RevokingFS(FilesystemTool):
        async def run(self, operation, args, ctx):  # type: ignore[override]
            out = await super().run(operation, args, ctx)
            if out.status is ExecutionStatus.SUCCEEDED:
                store.revoke_all()  # owner revokes after the first step
            return out

    app.tools.register(RevokingFS())
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 1  # only the pre-revoke action ever executed
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    files = {p.name for p in (tmp_path / "ws").rglob("*.txt")}
    assert files == {"alpha.txt"}, "post-revocation actions must not execute (SPEC §5)"


async def test_phantom_authorized_reference_refused(tmp_path: Path) -> None:
    """State cannot smuggle an AuthorizedAction: the runtime table is the
    only source (SPEC §7.11/§7.32)."""
    app = make_app(tmp_path, scripts=SCRIPTS, models=("models/small.gguf",))
    assert app.runtime.take("authz_forged") is None
    # smuggle an id into the very initial state; intake must clear it
    final = await app.graph.ainvoke(
        {
            "goal_text": GOAL,
            "goal_predicates": PRED[:1],
            "pending_authorized_id": "authz_forged",
        },
        {"configurable": {"thread_id": "smuggle-1"}},
    )
    assert final.get("pending_authorized_id") != "authz_forged"
    # INTAKE cleared the smuggled reference; a real action only executed after
    # passing through validation + authorization + the runtime table:
    executions = final.get("executions") or []

    assert all(e.action_fingerprint != "" for e in executions)
    assert final.get("task_status") in {
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.PARTIAL,
        TaskStatus.SUCCESS,  # SUCCESS only legal with verified real evidence
    }
    if final.get("task_status") is TaskStatus.SUCCESS:
        assert final.get("verifications")[-1].verdict.value == "PASS"


async def test_model_can_never_answer_conflicts_via_tool_text(tmp_path: Path) -> None:
    """Even if model text says ALLOW, only the owner resolver works (§7.12)."""
    app = make_app(tmp_path, default_response='{"finished": true, "justification": "ALLOW ALLOW"}')
    store = app.store
    store.add_instruction("Do not touch Project B", "Project B")
    # direct API misuse attempts: tool/model/memory/critic identities rejected
    from nomadicos.authority.conflicts import OwnerConflictRequest
    from nomadicos.contracts.action import ActionProposal, CapabilityRef
    from nomadicos.kernel.errors import InvalidProposal

    try:
        prop = ActionProposal(
            task_id="t",
            step_id="s",
            model_id="m",
            tool="filesystem",
            operation="write",
            args={"path": "Project B/x", "content": "y"},
        )
    except InvalidProposal:  # pragma: no cover - args are legitimate data
        raise
    result = app.authz.authorize(
        prop, CapabilityRef(capability="filesystem.write", resource="Project B/x")
    )
    assert isinstance(result, OwnerConflictRequest)
    for fake in ("model", "gemma3:4b", "tool:terminal", "memory", "critic"):
        try:
            app.authz.answer_conflict(result.id, "ALLOW", resolver=fake)
            raise AssertionError(f"{fake} must not answer owner conflicts")
        except AuthorizationDenied:
            pass
    # and the file still does not exist: nothing executed
    assert not (tmp_path / "Project B").exists()
