"""Focused regression coverage: proposal-validation recovery.

The real failure: the model hallucinated ``press_key.press_key`` and the
identical INVALID_PROPOSAL failure was retried three times — because
proposal-validation failures NEVER registered a fingerprint, so the §31
identical-repeat escalation never fired, and the replanning layer had no
dedicated rejection context. Fixed: the invalid proposal's fingerprint
registers (identical repeats escalate at the bounded threshold), and the
retry prompt exposes the EXACT validation error plus the registered action
names. The desktop contract itself (desktop.hotkey) is covered in
tests/desktop/test_hotkey.py.
"""

from __future__ import annotations

from pathlib import Path

from desktop_helpers import desktop_json, make_desktop_app  # tests/desktop

from nomadicos.contracts.core import TaskStatus

GOAL = "can you press 'win + d' key on keyboard"
HALLUCINATION = '{"tool": "press_key", "operation": "press_key", "args": {"key": "win+d"}}'


def prompts_after_first_rejection(app) -> list[str]:
    """All model prompts generated AFTER the first generate call."""
    return ["\n".join(m.content for m in r.messages) for r in app.mock.calls[1:]]


async def test_identical_invalid_proposals_escalate_not_retried_blindly(tmp_path: Path) -> None:
    """The same deterministic invalid proposal must NOT simply be retried
    three times: identical fingerprints escalate at the bounded threshold."""
    app = make_desktop_app(tmp_path, scripts=[("win + d", HALLUCINATION)])
    summary = await app.run_goal(GOAL)
    # the invalid proposal was rejected every time (fail-closed); nothing
    # reached the desktop backend
    backend = app.tools.get("desktop").backend
    assert backend.hotkeys == [] and backend.keys == []
    # identical fingerprints escalate BEFORE exhausting all 3 recoveries:
    # the stuck check fires at escalation_repeat_threshold (2), so the run
    # ends with FEWER wasted retries than the old blind 3x loop; after
    # escalation no other model is routable, so the task fails honestly
    assert summary.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)
    assert (summary.recoveries or 0) < 3
    # the identical-repeat escalation actually fired (graph state evidence)
    final = app.graph.get_state(
        {"configurable": {"thread_id": f"nomadic:{summary.task_id}"}}
    ).values
    assert list(final.get("escalation_count") or [])


async def test_rejection_context_exposed_to_replanning(tmp_path: Path) -> None:
    """After a rejected proposal, the retry prompt carries the EXACT
    validation error and the registered action names."""
    app = make_desktop_app(
        tmp_path,
        scripts=[
            (
                "win + d",
                [
                    HALLUCINATION,
                    desktop_json("hotkey", {"keys": ["win", "d"]}),
                    '{"finished": true}',
                ],
            )
        ],
    )
    summary = await app.run_goal(GOAL)
    later = prompts_after_first_rejection(app)
    assert later, "the model was re-prompted after the rejection"
    retry_prompt = later[0]
    # the EXACT validation error is exposed (untruncated rejection block)
    assert "YOUR LAST PROPOSAL WAS REJECTED" in retry_prompt
    assert "unknown action press_key.press_key" in retry_prompt
    # the available valid action names are exposed again in the user prompt
    assert 'desktop: operation "hotkey"' in retry_prompt
    assert "REGISTERED ACTIONS" in retry_prompt
    # with the rejection context, the corrected proposal converged: the
    # canonical desktop.hotkey action was authorized and executed
    backend = app.tools.get("desktop").backend
    assert backend.hotkeys == [["win", "d"]]
    assert summary.goal_verdict != "PASS"  # no predicate: no SUCCESS manufacture


async def test_valid_proposals_still_recover_normally(tmp_path: Path) -> None:
    """The fingerprint-on-failure fix does not change the happy path: a
    valid proposal is authorized, executed, and observed as before."""
    app = make_desktop_app(
        tmp_path,
        scripts=[
            ("win + d", [desktop_json("hotkey", {"keys": ["win", "d"]}), '{"finished": true}'])
        ],
    )
    summary = await app.run_goal(GOAL)
    backend = app.tools.get("desktop").backend
    assert backend.hotkeys == [["win", "d"]]
    assert summary.status.value in ("PARTIAL", "SUCCESS")
    assert summary.executions >= 1
