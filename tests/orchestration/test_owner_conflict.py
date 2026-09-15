"""Owner-conflict interrupt/resume semantics (SPEC §4-5, §7.12, §7.29)."""

from __future__ import annotations

from pathlib import Path

from helpers import make_app, write_json

from nomadicos.authority.conflicts import OwnerConflictRequest
from nomadicos.contracts.action import ActionProposal, CapabilityRef
from nomadicos.contracts.core import TaskStatus
from nomadicos.kernel.events import EventType

GOAL_B = "Update the changelog inside Project B"
PRED_B = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]
SCRIPT_B = [("CHANGELOG", write_json("Project B/CHANGELOG.md", "updated"))]


def _conflict_app(tmp_path: Path):
    app = make_app(tmp_path, scripts=SCRIPT_B, models=("models/small.gguf",))
    app.store.add_instruction("Do not touch Project B", "Project B")
    return app


async def test_authorize_conflict_pauses_graph_via_interrupt(tmp_path: Path) -> None:
    app = _conflict_app(tmp_path)
    summary = await app.run_goal(GOAL_B, predicates=PRED_B, task_id="conflict1")
    assert summary.waiting_owner(), summary
    assert summary.conflict and "Project B" in str(summary.conflict)
    # paused: the forbidden file was NOT written
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    events = [e.type for e in app.log.events(summary.task_id)]
    assert EventType.OWNER_CONFLICT_REQUESTED in events


async def test_invalid_owner_answer_reinterrupts_and_stays_blocked(tmp_path: Path) -> None:
    app = _conflict_app(tmp_path)
    summary = await app.run_goal(GOAL_B, predicates=PRED_B, task_id="conflict2")
    assert summary.waiting_owner()
    again = await app.resume_owner("conflict2", "sounds good to me")
    assert again.waiting_owner(), "garbage host input must not resolve authority"
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))


async def test_owner_allow_resumes_through_authoritative_path(tmp_path: Path) -> None:
    app = _conflict_app(tmp_path)
    first = await app.run_goal(GOAL_B, predicates=PRED_B, task_id="conflict3")
    assert first.waiting_owner()
    done = await app.resume_owner("conflict3", "ALLOW")
    # Phase 8: ALLOW only opens the authorization door - SUCCESS still required
    # independent evidence (the written, verified file), not the permission.
    assert done.status is TaskStatus.SUCCESS, done.outcome_note
    assert done.goal_verdict == "PASS", "SUCCESS here is earned by real evidence"
    written = list((tmp_path / "ws").rglob("CHANGELOG.md"))
    assert written and written[0].read_text(encoding="utf-8") == "updated"
    assert done.executions == 1
    decisions = [e for e in app.log.events(done.task_id) if e.type is EventType.OWNER_DECISION]
    assert [d.result for d in decisions] == ["ALLOW"]
    # single-use: the identical action must ASK again (override was consumed)
    same = ActionProposal(
        task_id=done.task_id,
        step_id="step_probe",
        model_id="models/small.gguf",
        tool="filesystem",
        operation="write",
        args={"path": "Project B/CHANGELOG.md", "content": "again"},
    )
    conflict2 = app.authz.authorize(
        same, CapabilityRef(capability="filesystem.write", resource="Project B/CHANGELOG.md")
    )
    assert isinstance(conflict2, OwnerConflictRequest)


async def test_owner_deny_leaves_action_blocked(tmp_path: Path) -> None:
    app = _conflict_app(tmp_path)
    first = await app.run_goal(GOAL_B, predicates=PRED_B, task_id="conflict4")
    assert first.waiting_owner()
    after = await app.resume_owner("conflict4", "DENY")
    assert after.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md")), "DENY => remains blocked (§5)"
    denials = [e for e in app.log.events(after.task_id) if e.type is EventType.OWNER_DECISION]
    assert [d.result for d in denials] == ["DENY"]
