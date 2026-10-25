"""Phase 2 brain-driven execution tests: outcome classification, safe
recovery, approval/revocation, and the semantic monitor gate.

These tests prove SAFETY PROPERTIES, not call ordering:
- unknown execution outcomes stop instead of replaying;
- confirmed side effects are never duplicated by retries;
- denials are classified as policy facts, not transient errors;
- approval/resume/revocation stay inside the existing authority path;
- the semantic monitor is fail-closed and bounded;
- only the authoritative goal verifier decides SUCCESS.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_app
from test_brain_integration import brain_scripts, candidate_json

from nomadicos.contracts.core import TaskStatus
from nomadicos.persistence.errors import PersistenceError

GOAL = "Create file report.txt containing HELLO-GRAPH"
PRED = [{"type": "file_exists", "path": "report.txt"}]


def failing_command_candidate() -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "id": "c1",
                    "kind": "EXECUTE",
                    "subgoal_id": "s0",
                    "tool": "terminal.execute",
                    "arguments": {"command": "cmd", "args": ["/c", "exit", "1"]},
                    "preconditions": [],
                    "expected_effect": "command exits 1",
                    "rationale": "probe",
                }
            ]
        }
    )


def owner_write_candidate() -> str:
    return candidate_json(
        tool="filesystem.write",
        arguments={"path": "Project B/CHANGELOG.md", "content": "updated"},
        effect="changelog updated",
        rationale="owner asked for the changelog",
    )


class FailingPutLedger:
    """Simulates the crash window: the ledger can never record (§56.14)."""

    def __init__(self) -> None:
        self.puts = 0

    def get(self, key: str):  # type: ignore[no-untyped-def]
        return None

    def put(self, key: str, result) -> None:  # type: ignore[no-untyped-def]
        self.puts += 1
        raise PersistenceError("simulated durability gap: disk full")

    def claim_intent(self, key: str, intent) -> bool:  # type: ignore[no-untyped-def]
        # claim succeeds, but the result put will fail → crash gap
        return True

    def get_intent(self, key: str):  # type: ignore[no-untyped-def]
        return None

    def mark_intent_started(self, key: str) -> None:  # type: ignore[no-untyped-def]
        pass

    def mark_intent_completed(self, key: str) -> None:  # type: ignore[no-untyped-def]
        pass


def _brain_state_of(app, task_id: str):  # type: ignore[no-untyped-def]
    from nomadicos.orchestration.brain_adapter import load_brain_state

    values = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}}).values
    return load_brain_state(values.get("brain_state"))


def _recovery_records(app, task_id: str) -> list[dict]:
    snapshot = app.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}})
    return list(snapshot.values.get("recovery_count") or [])


async def test_unknown_outcome_stops_instead_of_replaying(tmp_path: Path) -> None:
    """An execution whose outcome the durable ledger cannot prove is
    UNKNOWN: recovery STOPS with an explicit uncertainty report instead of
    replaying the action (no blind replay, SPEC §56.14)."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(
            json.dumps(
                {
                    "candidates": [
                        {
                            "id": "c1",
                            "kind": "EXECUTE",
                            "subgoal_id": "s0",
                            "tool": "terminal.execute",
                            "arguments": {"command": "cmd", "args": ["/c", "exit", "1"]},
                            "preconditions": [],
                            "expected_effect": "command exits 1",
                            "rationale": "probe",
                        }
                    ]
                }
            )
        ),
    )
    app.executor._ledger = FailingPutLedger()  # noqa: SLF001 - test seam for the crash window
    summary = await app.run_goal("run a failing command", predicates=PRED)
    assert summary.status is TaskStatus.BLOCKED, summary.outcome_note
    assert "uncertain" in summary.outcome_note.lower()
    tool_starts = [e for e in app.log.events(summary.task_id) if e.type.value == "TOOL_STARTED"]
    # the outcome is UNKNOWN: exactly one real attempt, NO blind replay
    assert summary.executions == 1
    assert len(tool_starts) == 1


async def test_confirmed_side_effect_not_duplicated_after_verification_failure(
    tmp_path: Path,
) -> None:
    """The brain writes out.txt successfully; the goal predicate wants
    different content. Recovery re-proposes the SAME action, the durable
    ledger blocks the duplicate side effect, and the mission ends bounded
    with the side effect applied exactly once."""
    actor_replies = [
        candidate_json(
            cid="c1",
            tool="filesystem.write",
            arguments={"path": "out.txt", "content": "HELLO"},
            effect="out.txt with HELLO",
        )
    ]
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(actor_replies),
        default_response='{"finished": true}',
    )
    summary = await app.run_goal(GOAL, predicates=[{"type": "file_exists", "path": "report.txt"}])
    assert summary.status is not TaskStatus.SUCCESS
    duplicate = [
        e
        for e in app.log.events(summary.task_id)
        if e.type.value == "TOOL_EXECUTED" and e.result == "DUPLICATE_BLOCKED"
    ]
    assert duplicate, "identical re-proposal must be duplicate-blocked, not re-executed"
    written = list((tmp_path / "ws").rglob("out.txt"))
    assert written and written[0].read_text(encoding="utf-8") == "HELLO"


async def test_denial_classified_not_transient(tmp_path: Path) -> None:
    """Validation/authorization denials are classified as policy facts
    (never TRANSIENT) and the denied action never reaches the executor."""
    app = make_app(
        tmp_path,
        grant=False,
        brain_actor=True,
        scripts=brain_scripts(candidate_json()),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    events = app.log.events(summary.task_id)
    assert not any(e.type.value == "TOOL_STARTED" for e in events)
    records = _recovery_records(app, summary.task_id)
    assert records, "denial retries must be classified by the recovery gateway"
    assert all(r.get("outcome_class") in ("DENIAL", "NO_EXECUTION") for r in records), records
    assert all(r["outcome_class"] != "TRANSIENT" for r in records)


async def test_owner_approval_brain_pause_allow(tmp_path: Path) -> None:
    """Brain proposals use the SAME owner-approval interrupt: pending
    approval executes nothing; ALLOW resumes through reauthorization."""
    goal = "Update the changelog inside Project B"
    pred = [{"type": "file_exists", "path": "Project B/CHANGELOG.md"}]
    app = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(owner_write_candidate()))
    app.store.add_instruction("Do not touch Project B", "Project B")
    first = await app.run_goal(goal, predicates=pred, task_id="brain_appr1")
    assert first.waiting_owner()
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md")), "pending approval must not execute"
    done = await app.resume_owner("brain_appr1", "ALLOW")
    assert done.status is TaskStatus.SUCCESS, done.outcome_note
    assert done.goal_verdict == "PASS", "SUCCESS is earned by real evidence"
    assert done.executions == 1


async def test_owner_deny_brain_action_not_executed(tmp_path: Path) -> None:
    app = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(owner_write_candidate()))
    app.store.add_instruction("Do not touch Project B", "Project B")
    first = await app.run_goal(
        "Update the changelog inside Project B",
        predicates=[{"type": "file_exists", "path": "Project B/CHANGELOG.md"}],
    )
    assert first.waiting_owner()
    after = await app.resume_owner(first.task_id, "DENY")
    assert after.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    denials = [e for e in app.log.events(after.task_id) if e.type.value == "OWNER_DECISION"]
    assert [d.result for d in denials] == ["DENY"]


async def test_revocation_while_paused_cannot_produce_execution(tmp_path: Path) -> None:
    """A stale/revoked authorization never becomes executable: revoking
    while paused bumps the authority epoch; resume then refuses."""
    app = make_app(tmp_path, brain_actor=True, scripts=brain_scripts(owner_write_candidate()))
    app.store.add_instruction("Do not touch Project B", "Project B")
    first = await app.run_goal(
        "Update the changelog inside Project B",
        predicates=[{"type": "file_exists", "path": "Project B/CHANGELOG.md"}],
        task_id="brain_revoke",
    )
    assert first.waiting_owner()
    app.store.revoke_all()
    after = await app.resume_owner("brain_revoke", "ALLOW")
    assert not list((tmp_path / "ws").rglob("CHANGELOG.md"))
    assert after.status is not TaskStatus.SUCCESS


async def test_semantic_monitor_rejects_invalid_candidates(tmp_path: Path) -> None:
    """A structurally-valid candidate the SEMANTIC monitor rejects cannot
    execute; the loop is bounded and verdicts are recorded in the Brain."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json(), monitor_accept=False),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)
    # the semantic stage ran (bounded) and rejected every iteration
    monitor_calls = [
        c for c in app.mock.calls if "CANDIDATES AND CONTEXT" in c.messages[-1].content
    ]
    assert monitor_calls, "semantic monitor must have evaluated the candidate"
    # bounded: per-attempt refinement pairs times bounded attempt budget
    assert 0 < len(monitor_calls) <= 18, f"unbounded monitor calls: {len(monitor_calls)}"
    brain = _brain_state_of(app, summary.task_id)
    assert brain is not None and any(
        r.decision.value == "REJECTED" and r.stage.value == "SEMANTIC"
        for r in brain.monitor_results
    )


async def test_structurally_invalid_candidate_never_reaches_semantic_monitor(
    tmp_path: Path,
) -> None:
    """The deterministic structural layer is never bypassed: an EXECUTE
    candidate without a tool fails at the brain's typed validation and the
    semantic model is never consulted."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=brain_scripts(candidate_json(cid="c1", kind="EXECUTE", tool="", arguments={})),
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    monitor_calls = [
        c for c in app.mock.calls if "CANDIDATES AND CONTEXT" in c.messages[-1].content
    ]
    assert monitor_calls == [], "structurally invalid candidates must not reach the semantic stage"
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)


async def test_monitor_failure_cannot_bypass_pipeline(tmp_path: Path) -> None:
    """A monitor transport failure becomes a typed failure through bounded
    recovery - never a raw-model fallback execution."""
    app = make_app(
        tmp_path,
        brain_actor=True,
        scripts=[
            ("goal_objective", candidate_json()),
            ("CANDIDATES AND CONTEXT", RuntimeError("monitor model down")),
        ],
    )
    summary = await app.run_goal(GOAL, predicates=PRED)
    assert summary.executions == 0
    assert list((tmp_path / "ws").rglob("report.txt")) == []
    assert summary.status in (TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.PARTIAL)
    for request in app.mock.calls:  # type: ignore[attr-defined]
        assert "REGISTERED ACTIONS" not in request.messages[-1].content
    failures = app.log.events(summary.task_id)
    assert any(e.type.value == "BRAIN_PROPOSED" for e in failures)
