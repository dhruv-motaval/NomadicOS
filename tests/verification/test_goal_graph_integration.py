"""Graph integration: REAL executor -> REAL observation -> REAL verification.

Full production path with real filesystem and real OS processes; the
"model" is the scripted MockEngine (manually supplied proposals - clearly
labeled), so results are deterministic. The live-model equivalents are in
tests/hardware/test_goal_verification_live.py.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from nomadicos.contracts.core import TaskStatus
from nomadicos.kernel.events import EventType
from orchestration.helpers import make_app, term_json, write_json

# ------------------------------------------------------- positive full path


async def test_graph_reaches_success_only_via_verified_evidence(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"P8-EXACT").hexdigest()
    app = make_app(
        tmp_path,
        scripts=[
            ("file_sha256", json.dumps({"finished": True})),  # 2nd step: claim
            ("p8.txt", write_json("p8.txt", "P8-EXACT")),
        ],
    )
    summary = await app.run_goal(
        "Create file p8.txt containing P8-EXACT",
        predicates=[
            {"type": "file_content_equals", "path": "p8.txt", "content": "P8-EXACT"},
            {"type": "file_sha256", "path": "p8.txt", "sha256": digest},
        ],
        task_id="succ1",
    )
    assert summary.status is TaskStatus.SUCCESS
    assert summary.goal_verdict == "PASS" and summary.goal_verifier == "nomadic-goal-verifier-v1"
    files = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "ws").rglob("p8.txt")}
    assert files == {"p8.txt": "P8-EXACT"}
    goal_evid = [e for e in app.log.events("succ1") if e.type is EventType.GOAL_VERIFIED]
    assert goal_evid[-1].result == "PASS"
    assert "p8.txt" in json.dumps(goal_evid[-1].payload)  # attributable why()
    step_evid = [
        e
        for e in app.log.events("succ1")
        if e.type is EventType.VERIFICATION_RESULT and e.result == "PASS"
    ]
    assert step_evid, "steps must also carry their own verification audit"


async def test_two_predicate_goal_requires_both(tmp_path: Path) -> None:
    app = make_app(
        tmp_path,
        scripts=[
            ("one.txt", write_json("one.txt", "RIGHT")),
            ("two.txt", write_json("two.txt", "WRONG")),
        ],
    )
    summary = await app.run_goal(
        "Produce one.txt and two.txt",
        predicates=[
            {"type": "file_content_equals", "path": "one.txt", "content": "RIGHT"},
            {"type": "file_content_equals", "path": "two.txt", "content": "RIGHT"},
        ],
        task_id="succ2",
    )
    # goal must NOT pass while one required predicate fails (§8.9/§8.12)
    assert summary.status is not TaskStatus.SUCCESS
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.PARTIAL}
    assert summary.goal_verdict in {"NOT_PASS", None}


# --------------------------------------------------- action-succeeds-falsely


async def test_step_verifier_catches_wrong_content_after_action_success(
    tmp_path: Path,
) -> None:
    """§8.37-critical A: action SUCCEEDED, content false => step NOT_PASS =>
    recovery => FAILED/BLOCKED; never SUCCESS."""
    app = make_app(tmp_path, scripts=[("wrong.txt", write_json("wrong.txt", "NOT-THE-ASKED"))])
    summary = await app.run_goal(
        "Create wrong.txt",
        predicates=[
            {"type": "file_content_equals", "path": "wrong.txt", "content": "ASKED-CONTENT"}
        ],
        task_id="falseA",
    )
    assert summary.executions >= 1
    tool_event = next(e for e in app.log.events("falseA") if e.type is EventType.TOOL_EXECUTED)
    assert tool_event.result == "SUCCEEDED"  # the ACTION was successful...
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}  # ...the GOAL is not
    step_fail = [
        e
        for e in app.log.events("falseA")
        if e.type is EventType.VERIFICATION_RESULT and e.result == "NOT_PASS"
    ]
    assert step_fail, "step verifier must reject the false content"
    assert all(
        e.result != "PASS" for e in app.log.events("falseA") if e.type is EventType.GOAL_VERIFIED
    )
    disk = (tmp_path / "ws" / "falseA" / "wrong.txt").read_text(encoding="utf-8")
    assert disk == "NOT-THE-ASKED"  # real write really happened


async def test_goal_verifier_rejects_false_content(tmp_path: Path) -> None:
    """§8.37-critical B: grouped predicate (no per-step expectation) routes
    the falseness to the GOAL verifier: NOT_PASS -> never SUCCESS."""
    app = make_app(tmp_path, scripts=[("w2.txt", write_json("w2.txt", "NOT-THE-ASKED"))])
    summary = await app.run_goal(
        "Create w2.txt",
        predicates=[
            {
                "any": [
                    {"type": "file_content_equals", "path": "w2.txt", "content": "ASKED"},
                    {"type": "file_content_equals", "path": "w2.txt", "content": "ALSO-WRONG"},
                ]
            }
        ],
        task_id="falseB",
    )
    assert summary.executions >= 1
    assert summary.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
    goal_events = [e for e in app.log.events("falseB") if e.type is EventType.GOAL_VERIFIED]
    assert goal_events and goal_events[-1].result == "NOT_PASS"


# ------------------------------------------------- claim vs evidence matrix


async def test_claim_true_with_evidence_fail_is_not_success(tmp_path: Path) -> None:
    """SPEC §8.15: completion_claim True + predicate false => NOT SUCCESS."""
    app = make_app(
        tmp_path,
        scripts=[("Satisfy", write_json("c.txt", "BAD"))],
        default_response='{"finished": true, "justification": "trusted me"}',
    )
    summary = await app.run_goal(
        "Create c.txt properly",
        predicates=[{"type": "file_content_equals", "path": "c.txt", "content": "GOOD"}],
        task_id="claimfail",
    )
    assert summary.status is not TaskStatus.SUCCESS


async def test_claim_false_with_evidence_pass_may_succeed(tmp_path: Path) -> None:
    """Evidence dominates claims: no claim at all, plan completes the file."""
    app = make_app(tmp_path, scripts=[("d.txt", write_json("d.txt", "FINE"))])
    summary = await app.run_goal(
        "Create file d.txt",
        predicates=[{"type": "file_exists", "path": "d.txt"}],
        task_id="claimpass",
    )
    assert summary.status is TaskStatus.SUCCESS  # claim never needed


# --------------------------------------------------- real process via graph


async def test_real_process_evidence_verified_by_goal(tmp_path: Path) -> None:
    payload = term_json(sys.executable, "-c", "print('PHASE8-OUT')")
    # output correlation from CAPTURED evidence - the verifier reruns nothing
    app = make_app(tmp_path, scripts=[("Satisfy", payload)])
    summary = await app.run_goal(
        "Run the print command in the current step",
        predicates=[{"type": "stdout_contains", "text": "PHASE8-OUT", "command": sys.executable}],
        task_id="proc1",
    )
    assert summary.executions == 1
    assert summary.goal_verdict == "PASS"
    assert summary.status is TaskStatus.SUCCESS


# ------------------------------------------- §8.20 no hallucinated evidence


async def test_no_hallucinated_evidence_end_to_end(tmp_path: Path) -> None:
    """Model may SAY files exist; the verifier reads the disk, not chat."""
    brag = json.dumps(
        {
            "tool": "filesystem",
            "operation": "write",
            "args": {"path": "ghost_note.txt", "content": "ghost.txt exists, tests pass, verified"},
        }
    )
    app = make_app(
        tmp_path,
        scripts=[("Satisfy", brag)],  # never creates ghost.txt itself
        default_response='{"finished": true, "justification": "ghost.txt exists, verified"}',
    )
    summary = await app.run_goal(
        "Create ghost.txt then confirm",
        predicates=[{"type": "file_exists", "path": "ghost.txt"}],
        task_id="halluc1",
    )
    assert summary.status is not TaskStatus.SUCCESS
    assert not (tmp_path / "ws" / "halluc1" / "ghost.txt").exists()
    assert summary.goal_verdict in {"NOT_PASS", None}
