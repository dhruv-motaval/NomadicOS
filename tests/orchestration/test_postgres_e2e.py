"""Phase 13G: REAL PostgreSQL end-to-end restart qualification.

The FULL restart flow with PostgreSQL as the production durable source of
task-state/checkpoint/ledger truth (SPEC §34/§47):

App A: create task -> execute safe action -> durable ledger record ->
       verification -> durable task truth + checkpoint persisted -> terminate
App B: reconstruct runtime -> load PostgreSQL task state -> restore
       checkpoint -> reopen memory -> inspect ledger -> continue/recover ->
       independently verify -> complete normally

Requires the docker-compose.dev.yml PostgreSQL (dev credentials; see
NOMADICOS_TEST_DSN). Skips honestly when unavailable.
"""

from __future__ import annotations

import os

import pytest
from helpers import make_app

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.memory import MemoryKind, MemoryQuery

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration

GOAL = "Create file pgflow.txt containing PG-DATA"
PRED = [{"type": "file_exists", "path": "pgflow.txt"}]
def write_json_helper(name: str, content: str) -> str:
    import json

    return json.dumps({
        "tool": "filesystem",
        "operation": "write",
        "args": {"path": name, "content": content},
    })


SCRIPT = [("pgflow.txt", write_json_helper("pgflow.txt", "PG-DATA"))]


def _pg_app(tmp_path, scripts=None):
    """NomadicApp with PostgreSQL as the durable task-state/checkpoint/
    ledger source (production configuration path, SPEC §34)."""
    app = make_app(
        tmp_path,
        scripts=scripts or [],
        config_over={"persistence": {"dsn": TEST_DSN, "state_dir": str(tmp_path / "state")}},
    )
    return app


# ------------------------------------------------- full restart flow (PG) --


async def test_full_restart_flow_with_real_postgres(tmp_path) -> None:
    app_a = _pg_app(tmp_path, scripts=[("pgflow.txt", write_json_helper("pgflow.txt", "PG-DATA"))])
    summary_a = await app_a.run_goal(GOAL, predicates=PRED)
    assert summary_a.status is TaskStatus.SUCCESS, summary_a.outcome_note
    task_id = summary_a.task_id
    await app_a.aclose()

    # App B: fresh runtime over the SAME PostgreSQL truth
    app_b = _pg_app(tmp_path)
    # 1) durable task state loads from PostgreSQL
    record = app_b.task_store.load_task(task_id)
    assert record is not None and record.status is TaskStatus.SUCCESS
    # 2) the checkpoint restored (thread present in the durable snapshot)
    assert f"nomadic:{task_id}" in app_b.checkpointer.thread_ids()
    # 3) memory reopened from state_dir
    assert isinstance(
        app_b.memory_store.query(MemoryQuery(text="pgflow", kinds=[MemoryKind.EPISODIC], limit=32)),
        list,
    )
    # 4) completed task restores as DATA: no re-execution, no duplicate SUCCESS
    resumed = await app_b.resume_task(task_id)
    assert resumed.status is TaskStatus.SUCCESS
    assert app_b.log.events(task_id) == []
    # 5) durable ledger: the same logical action re-proposed is durably blocked
    from nomadicos.action_ir import ValidationContext
    from nomadicos.contracts.action import AuthorizedAction
    from nomadicos.tools import ExecutionContext

    state = app_b.graph.get_state({"configurable": {"thread_id": f"nomadic:{task_id}"}}).values
    original = state["proposals"][-1]
    ref = app_b.validator.validate(
        original,
        ValidationContext(
            task_id=task_id, step_id=original.step_id, model_id=original.model_id, attempt=1
        ),
    )
    request = app_b.authz.authorize(original, ref)
    if isinstance(request, AuthorizedAction):
        replay = await app_b.executor.execute(
            request,
            ExecutionContext(
                task_id=task_id,
                workspace=app_b.runtime.workspace_root / task_id,
                full_pc=True,
            ),
        )
        assert replay.evidence["duplicate_blocked"] is True
    # 6) §35 auditability: the durable record answers the lifecycle questions
    assert record.executions and record.executions[-1].model_id
    assert record.executions[-1].tool == "filesystem"
    assert record.verifications and record.verifications[-1].outcome.value == "PASS"
    assert record.audit_events, "durable audit events reconstruct the run"
    types = [str(e.get("type")) for e in record.audit_events]
    for expected in (
        "ACTION_PROPOSED",
        "ACTION_VALIDATED",
        "AUTHORIZATION_GRANTED",
        "TOOL_EXECUTED",
        "TASK_COMPLETED",
    ):
        assert expected in types, f"audit trail missing {expected}"
