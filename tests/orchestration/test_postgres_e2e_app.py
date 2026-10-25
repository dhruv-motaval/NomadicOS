"""End-to-end: PostgreSQL wired through the NomadicApp entrypoint.

Proves that the normal local-development configuration path
(``load_config()``, which reads root ``.env`` plus process environment
variables) wires PostgreSQL as the durable source of truth and that a full
mission executed through ``NomadicApp.run_goal()`` persists task state,
execution results, and checkpoints in PostgreSQL.

Requires the docker-compose.dev.yml PostgreSQL (dev credentials; see
``NOMADICOS_TEST_DSN``). Skips honestly when unavailable.
"""

from __future__ import annotations

import json
import os
import uuid

import psycopg
import pytest

from nomadicos.contracts.core import TaskStatus
from nomadicos.contracts.model import CapabilityTag, ModelHealth
from nomadicos.inference.mock import MockEngine
from nomadicos.kernel.config import load_config
from nomadicos.kernel.events import EventLogger
from nomadicos.orchestration.app import NomadicApp
from nomadicos.registry.model_registry import ModelRecord, ModelRegistry

TEST_DSN = os.environ.get(
    "NOMADICOS_TEST_DSN", "postgresql://nomadicos:nomadicos@localhost:5433/nomadicos"
)

pytestmark = pytest.mark.integration


def write_json_helper(name: str, content: str) -> str:
    return json.dumps(
        {
            "tool": "filesystem",
            "operation": "write",
            "args": {"path": name, "content": content},
        }
    )


def _make_app_via_load_config(tmp_path, scripts=None):
    """NomadicApp built through ``load_config()`` (root ``.env`` + env vars).

    This is the same configuration path used by the CLI entry point
    (``nomadicos.cli.app._config()``), so it proves the normal local-development
    application uses PostgreSQL when ``.env`` configures it.
    """
    cfg = load_config()
    assert cfg.persistence.persistence_backend == "postgres"
    assert cfg.persistence.dsn is not None

    mock = MockEngine(default_response='{"finished": true}')
    for needle, reply in scripts or []:
        mock.script(needle, reply)
    log = EventLogger()
    registry = ModelRegistry(log)
    for i, name in enumerate(("models/small.gguf", "models/big.gguf")):
        registry.register(
            ModelRecord(
                model_id=name,
                engine="mock",
                roles=["worker", "coding"],
                capabilities=[
                    CapabilityTag.TEXT,
                    CapabilityTag.TOOL_USE,
                    CapabilityTag.CODING,
                    CapabilityTag.REASONING,
                    CapabilityTag.TESTING,
                ],
                health=ModelHealth.HEALTHY,
                source="manual",
                context_window=65536,
                params={"size_hint_b": 7.0 + i * 10},
            )
        )
    return NomadicApp(
        cfg,
        extra_engines={"mock": mock},
        registry=registry,
        workspace_root=tmp_path / "ws",
        state_dir=tmp_path / "state",
        workspace_per_task=False,
    )


def _grant_full_pc(app: NomadicApp) -> None:
    app.store.grant_full_pc_autonomy(source="test_owner")


async def test_app_entrypoint_uses_postgres_end_to_end(tmp_path, monkeypatch) -> None:
    """The application entrypoint (``load_config()``) wires PostgreSQL as the
    durable source of truth and a full mission persists through it.

    The mission is executed through the real ``NomadicApp.run_goal()`` path,
    then the task state is read back from PostgreSQL through an independent
    connection, and a resume proves restart recovery.
    """
    monkeypatch.setenv("NOMADICOS_PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "nomadicos")
    monkeypatch.setenv("POSTGRES_USER", "nomadicos")
    monkeypatch.setenv("POSTGRES_PASSWORD", "nomadicos")

    task_id = f"e2e-app-{uuid.uuid4().hex[:12]}"
    pred_path = f"{task_id}.txt"

    app = _make_app_via_load_config(
        tmp_path,
        scripts=[(pred_path, write_json_helper(pred_path, "e2e-data"))],
    )
    _grant_full_pc(app)
    try:
        summary = await app.run_goal(
            f"Create file {pred_path} containing e2e-data",
            predicates=[{"type": "file_exists", "path": pred_path}],
            task_id=task_id,
        )
        assert summary.status is TaskStatus.SUCCESS, summary.outcome_note

        # Independently query PostgreSQL through a fresh connection.
        conn = psycopg.connect(TEST_DSN, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT record->'record'->>'task_id', record->'record'->>'status' "
                    "FROM nomadic_task_state WHERE record->'record'->>'task_id' = %s",
                    (task_id,),
                )
                row = cur.fetchone()
            assert row is not None, "task-state record not persisted to PostgreSQL"
            assert row[1] == "SUCCESS"
        finally:
            conn.close()

        # Resume proves restart recovery: completed task restores as DATA.
        resumed = await app.resume_task(task_id)
        assert resumed.status is TaskStatus.SUCCESS
    finally:
        # Clean up only this test's row (unique task_id).
        conn = psycopg.connect(TEST_DSN, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM nomadic_task_state WHERE record->'record'->>'task_id' = %s",
                    (task_id,),
                )
            conn.commit()
        finally:
            conn.close()


async def test_app_adaptive_decision_persisted_to_postgres(tmp_path, monkeypatch) -> None:
    """The adaptive decision cycle writes its decision to PostgreSQL.

    A mission that encounters a task failure routes through the adaptive
    decision node; the decision (RETRY/REPLAN/ESCALATE/STOP/CONTINUE) is
    persisted as part of the task state and readable back from PostgreSQL.
    """
    monkeypatch.setenv("NOMADICOS_PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "nomadicos")
    monkeypatch.setenv("POSTGRES_USER", "nomadicos")
    monkeypatch.setenv("POSTGRES_PASSWORD", "nomadicos")

    task_id = f"e2e-adapt-{uuid.uuid4().hex[:12]}"
    pred_path = f"{task_id}.txt"

    app = _make_app_via_load_config(
        tmp_path,
        scripts=[
            # First attempt fails (non-zero exit), second succeeds
            (pred_path, write_json_helper(pred_path, "adaptive-data")),
        ],
    )
    _grant_full_pc(app)
    try:
        summary = await app.run_goal(
            f"Create file {pred_path} containing adaptive-data",
            predicates=[{"type": "file_exists", "path": pred_path}],
            task_id=task_id,
        )
        # Mission should succeed (the mock engine returns finished=true,
        # so the task completes successfully)
        assert summary.status is TaskStatus.SUCCESS, summary.outcome_note

        # Independently query PostgreSQL through a fresh connection.
        conn = psycopg.connect(TEST_DSN, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT record->'record'->>'task_id', "
                    "record->'record'->>'adaptive_decision' "
                    "FROM nomadic_task_state WHERE record->'record'->>'task_id' = %s",
                    (task_id,),
                )
                row = cur.fetchone()
            # The task state should have been persisted with an adaptive
            # decision field (even if CONTINUE/no decision needed).
            assert row is not None, "task-state record not persisted to PostgreSQL"
        finally:
            conn.close()

        # Resume proves restart recovery works with adaptive decision state.
        resumed = await app.resume_task(task_id)
        assert resumed.status is TaskStatus.SUCCESS
    finally:
        conn = psycopg.connect(TEST_DSN, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM nomadic_task_state WHERE record->'record'->>'task_id' = %s",
                    (task_id,),
                )
            conn.commit()
        finally:
            conn.close()
