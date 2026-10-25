"""Phase 12B graph-level end-to-end: desktop actions through the REAL
orchestration graph (propose -> validate -> authorize -> execute -> observe
-> verify -> GoalVerifier -> SUCCESS), with a deterministic fake backend.

The critical negative: a successful desktop ACTION does NOT make the task
SUCCESS — only independent goal verification does."""

from __future__ import annotations

from pathlib import Path

from desktop_helpers import desktop_json, make_desktop_app

from nomadicos.kernel.events import EventType

FOCUS_GOAL = "Focus the Notepad window"


def success_events(app, task_id: str) -> list:
    return [
        e
        for e in app.log.events(task_id)
        if e.type is EventType.TASK_COMPLETED and e.result == "SUCCESS"
    ]


# --------------------------------------------------------------- SUCCESS ----


async def test_focus_window_goal_reaches_verified_success(tmp_path: Path) -> None:
    app = make_desktop_app(
        tmp_path,
        scripts=[("Notepad", desktop_json("focus_window", {"title": "notepad"}))],
    )
    backend = app.tools.get("desktop").backend
    summary = await app.run_goal(
        FOCUS_GOAL,
        predicates=[{"type": "window_present", "title": "notepad"}],
    )
    assert summary.status.value == "SUCCESS", summary.outcome_note
    assert summary.goal_verdict == "PASS"
    assert backend.focused == "Untitled - Notepad"
    assert summary.executions == 1
    completed = success_events(app, summary.task_id)
    assert len(completed) == 1, "exactly one SUCCESS writer, used once"
    order = [e.type for e in app.log.events(summary.task_id)]
    for expected in (
        EventType.ACTION_PROPOSED,
        EventType.ACTION_VALIDATED,
        EventType.AUTHORIZATION_GRANTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_EXECUTED,
        EventType.VERIFICATION_RESULT,
    ):
        assert expected in order


# ---------------------------------------------------- negative: no SUCCESS --


async def test_click_success_without_window_evidence_is_not_goal_success(
    tmp_path,
) -> None:
    """Critical Phase 12 distinction: the desktop action SUCCEEDS but the
    requested UI state is never evidenced -> NO SUCCESS."""
    app = make_desktop_app(
        tmp_path,
        scripts=[("Click", desktop_json("mouse_click", {"x": 10, "y": 20}))],
    )
    backend = app.tools.get("desktop").backend
    summary = await app.run_goal(
        "Click the center of the screen",
        predicates=[{"type": "window_present", "title": "notepad"}],
    )
    assert summary.status.value != "SUCCESS"
    assert summary.goal_verdict in ("NOT_PASS", "NOT_VERIFIED")
    # the click DID happen — but the requested UI state was never evidenced
    assert backend.clicks == [(10, 20, "left", 1)]
    assert not success_events(app, summary.task_id)


async def test_observed_window_without_match_is_not_pass(tmp_path: Path) -> None:
    app = make_desktop_app(
        tmp_path,
        scripts=[("Notepad window", desktop_json("list_windows", {}))],
    )
    backend = app.tools.get("desktop").backend
    backend.windows = [{"handle": 1, "title": "Some Other Window"}]
    summary = await app.run_goal(
        "Show me the Notepad window",
        predicates=[{"type": "window_present", "title": "notepad"}],
    )
    assert summary.status.value != "SUCCESS"
    assert summary.goal_verdict == "NOT_PASS"
    assert backend.windows[0]["title"] == "Some Other Window"
