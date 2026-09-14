"""STEP 4: persistence is authoritative. These tests exercise the REAL
PostgreSQL mirror of the lifecycle (skipped when the dev database is down)."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from nomadicos.agent.runtime import AgentRuntime
from nomadicos.agent.selector import HardwareConstraints, ModelSelector
from nomadicos.audit.fake import FakeAuditSink
from nomadicos.constitution.policy_loader import PolicyEngine
from nomadicos.core.config import CoreConfig
from nomadicos.core.errors import StateConflict
from nomadicos.core.lifecycle import TaskStatus
from nomadicos.evaluation.engine import EvaluationEngine
from nomadicos.evaluation.model_eval import ModelPerformanceTracker
from nomadicos.experience.recorder import ExperienceRecorder
from nomadicos.experience.store import InMemoryExperienceStore
from nomadicos.models.base import ModelStatus
from nomadicos.models.fake import FakeLocalModel
from nomadicos.models.manager import ModelManager
from nomadicos.postgres.client import PostgresClient, client_from_config
from nomadicos.postgres.migrator import MigrationRunner
from nomadicos.postgres.repositories import TaskRepository
from nomadicos.security.budgets import TaskBudget
from nomadicos.security.gate import SecurityGate
from nomadicos.security.permissions import PermissionEngine, SubjectIdentity
from nomadicos.tools.filesystem import FilesystemTool
from nomadicos.tools.gateway import ToolGateway

MIGRATIONS = Path(__file__).parents[2] / "src/nomadicos/postgres/migrations"
USER = "step4-user"

_POLICY_TMPL = (
    'version: "1.0.0"\n'
    "owner:\n"
    "  autonomy_level: autonomous\n"
    "  tools:\n"
    "    - id: fs-rule\n"
    "      tool: filesystem\n"
    "      risk: medium\n"
    "      default_decision: allow\n"
    "  external_network:\n"
    "    allow_public_get: true\n"
)
WRITE = json.dumps(
    {
        "tool": "filesystem",
        "arguments": {"action": "write", "path": "d.txt", "content": "db"},
        "finished": False,
    }
)


def _password() -> str:
    return os.environ.get("POSTGRES_PASSWORD", "")


def _client() -> PostgresClient:
    # Mirror production exactly: Runtime._load_dotenv() FIRST, then config.
    from nomadicos.core.runtime import Runtime

    Runtime._load_dotenv()
    client = client_from_config(CoreConfig(), _password())
    client.connect()
    if not client.ping():
        raise ConnectionError("db unavailable")
    return client


@pytest.fixture()
def db() -> PostgresClient:
    try:
        client = _client()
    except Exception:
        pytest.skip("PostgreSQL not reachable (dev DB down)")
    MigrationRunner(client, MIGRATIONS).run()
    yield client
    client.execute("DELETE FROM nomadicos.tasks WHERE user_id = %s", (USER,))
    client.close()


def _row(client, task_id) -> str:
    rows = client.execute(
        "SELECT status FROM nomadicos.tasks WHERE task_id = %s",
        (uuid.UUID(str(task_id)),),
    )
    return rows[0]["status"]


def _runtime(client, responses, tmp_path):
    policy = PolicyEngine()
    f = tmp_path / "owner.yaml"
    f.write_text(_POLICY_TMPL, encoding="utf-8")
    policy.load_file(f)
    gate = SecurityGate(policy, PermissionEngine(), FakeAuditSink())
    gw = ToolGateway(gate, FakeAuditSink())
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gw.register(FilesystemTool(workspace_root=str(ws)))
    m = FakeLocalModel("fake/planner")
    m._responses = list(responses)
    mgr = ModelManager()
    mgr.register(m, status=ModelStatus.ENABLED)
    repo = TaskRepository(client)

    async def sink(tid: str, expected: TaskStatus, to: TaskStatus) -> None:
        await repo.advance_status(uuid.UUID(tid), expected, to)

    rt = AgentRuntime(
        selector=ModelSelector(
            mgr, ModelPerformanceTracker(), hardware=HardwareConstraints(ram_mb=8192)
        ),
        manager=mgr,
        gateway=gw,
        audit_sink=FakeAuditSink(),
        recorder=ExperienceRecorder(InMemoryExperienceStore()),
        evaluator=EvaluationEngine(),
        budget=TaskBudget(max_steps=3, max_retries=1),
        workspace_root=str(ws),
    )
    return rt, sink


def test_create_row_is_created_and_cas_advances(db) -> None:
    repo = TaskRepository(db)
    tid = asyncio.run(repo.create(USER, "cas goal"))
    assert _row(db, tid) == "CREATED"
    asyncio.run(repo.advance_status(tid, TaskStatus.CREATED, TaskStatus.PLANNED))
    assert _row(db, tid) == "PLANNED"
    with pytest.raises(StateConflict):  # stale writer cannot overwrite authority
        asyncio.run(repo.advance_status(tid, TaskStatus.CREATED, TaskStatus.RUNNING))


def test_runtime_and_db_agree_after_success(db, tmp_path) -> None:
    tid = asyncio.run(TaskRepository(db).create(USER, "success goal"))
    rt, sink = _runtime(db, [WRITE, '{"finished": true}'], tmp_path)
    ident = SubjectIdentity(user_id=USER, session_id="s", task_id=str(tid))
    rep = asyncio.run(rt.execute_task("Write d.txt please", ident, max_steps=4, state_sink=sink))
    assert rep.status is TaskStatus.SUCCESS
    assert _row(db, tid) == "SUCCESS"  # runtime == DB: the STEP-2 bug, gone
    assert rep.status.value == _row(db, tid)


def test_runtime_and_db_agree_after_failure(db, tmp_path) -> None:
    tid = asyncio.run(TaskRepository(db).create(USER, "fail goal"))
    rt, sink = _runtime(db, ["not json"] * 6, tmp_path)
    ident = SubjectIdentity(user_id=USER, session_id="s", task_id=str(tid))
    rep = asyncio.run(rt.execute_task("Do the hopeless thing", ident, max_steps=2, state_sink=sink))
    assert rep.status is TaskStatus.FAILED
    assert _row(db, tid) == "FAILED"


def test_runtime_and_db_agree_after_controlled_tool_failure(db, tmp_path) -> None:
    """PART H — Controlled tool failure through production runtime + PostgreSQL."""
    fail_tool_call = json.dumps(
        {
            "tool": "filesystem",
            "arguments": {"action": "read", "path": "missing_audit_target.txt"},
            "finished": False,
        }
    )
    tid = asyncio.run(TaskRepository(db).create(USER, "tool failure goal"))
    rt, sink = _runtime(db, [fail_tool_call] * 6, tmp_path)
    ident = SubjectIdentity(user_id=USER, session_id="s", task_id=str(tid))
    rep = asyncio.run(rt.execute_task("Read missing file", ident, max_steps=2, state_sink=sink))
    assert rep.status is TaskStatus.FAILED
    assert _row(db, tid) == "FAILED"
    assert any("file does not exist" in f for f in rep.failed)
    # Verification never claimed on tool failure
    assert len(rep.verification) == 0


def test_duplicate_completion_lost_race_raises(db) -> None:
    repo = TaskRepository(db)
    tid = asyncio.run(repo.create(USER, "race goal"))
    asyncio.run(repo.advance_status(tid, TaskStatus.CREATED, TaskStatus.RUNNING))
    asyncio.run(repo.advance_status(tid, TaskStatus.RUNNING, TaskStatus.SUCCESS))
    with pytest.raises(StateConflict):
        asyncio.run(repo.advance_status(tid, TaskStatus.RUNNING, TaskStatus.FAILED))
    assert _row(db, tid) == "SUCCESS"


def test_reconcile_marks_interrupted_failed_keeps_blocked(db) -> None:
    repo = TaskRepository(db)
    hung = asyncio.run(repo.create(USER, "hung goal"))
    asyncio.run(repo.advance_status(hung, TaskStatus.CREATED, TaskStatus.PLANNED))
    asyncio.run(repo.advance_status(hung, TaskStatus.PLANNED, TaskStatus.RUNNING))

    parked = asyncio.run(repo.create(USER, "blocked goal"))
    asyncio.run(repo.advance_status(parked, TaskStatus.CREATED, TaskStatus.PLANNED))
    asyncio.run(repo.advance_status(parked, TaskStatus.PLANNED, TaskStatus.AUTHORIZED))
    asyncio.run(repo.advance_status(parked, TaskStatus.AUTHORIZED, TaskStatus.RUNNING))
    asyncio.run(repo.advance_status(parked, TaskStatus.RUNNING, TaskStatus.BLOCKED))

    fixed = asyncio.run(repo.reconcile_interrupted())
    assert str(hung) in fixed
    assert _row(db, hung) == "FAILED"  # never silently "completed"
    assert _row(db, parked) == "BLOCKED"  # awaiting owner survives restart
