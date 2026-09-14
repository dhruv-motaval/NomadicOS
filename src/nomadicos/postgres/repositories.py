"""Domain repositories (BP §93): the ONLY SQL surface. Agents never see SQL."""

import json
from typing import Any
from uuid import UUID, uuid4

from nomadicos.audit.base import AuditEvent, AuditEventCategory
from nomadicos.core.errors import StateConflict, ValidationError
from nomadicos.core.lifecycle import INTERRUPT_STATUSES, TaskStatus
from nomadicos.postgres.client import PostgresClient


class SessionRepository:
    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def create(
        self,
        user_id: str,
        *,
        session_id: UUID | None = None,
        title: str | None = None,
        project_id: str | None = None,
        isolation_mode: str = "shared_memory",
    ) -> UUID:
        sid = session_id or uuid4()
        self._client.execute(
            "INSERT INTO nomadicos.sessions (session_id, user_id, title, "
            "project_id, isolation_mode) "
            "VALUES (%s, %s, %s, %s, %s)",
            (sid, user_id, title, project_id, isolation_mode),
        )
        return sid

    async def get(self, session_id: UUID) -> dict[str, Any] | None:
        rows = self._client.execute(
            "SELECT * FROM nomadicos.sessions WHERE session_id = %s", (session_id,)
        )
        return rows[0] if rows else None


class TaskRepository:
    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def create(
        self,
        user_id: str,
        goal: str,
        *,
        session_id: UUID | None = None,
        priority: str = "NORMAL",
        task_id: UUID | None = None,
    ) -> UUID:
        if not goal.strip():
            raise ValidationError("task goal must be non-empty")
        tid = task_id or uuid4()
        self._client.execute(
            "INSERT INTO nomadicos.tasks (task_id, session_id, user_id, goal, priority, status) "
            "VALUES (%s, %s, %s, %s, %s, 'CREATED')",
            (tid, session_id, user_id, goal, priority),
        )
        return tid

    async def get(self, task_id: UUID) -> dict[str, Any] | None:
        rows = self._client.execute("SELECT * FROM nomadicos.tasks WHERE task_id = %s", (task_id,))
        return rows[0] if rows else None

    async def recent(self, limit: int = 3) -> list[dict[str, Any]]:
        """Most recent tasks (cross-session conversation seeding, BP §376)."""
        return self._client.execute(
            "SELECT goal, status FROM nomadicos.tasks ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )

    async def set_status(self, task_id: UUID, status: TaskStatus) -> None:
        terminal = {
            TaskStatus.SUCCESS,
            TaskStatus.FAILED,
            TaskStatus.PARTIALLY_COMPLETED,
            TaskStatus.CANCELLED,
        }
        if status not in terminal:
            raise ValidationError(
                f"use the runtime state machine; repository only records terminal status: {status}"
            )
        self._client.execute(
            "UPDATE nomadicos.tasks SET status = %s, updated_at = now() WHERE task_id = %s",
            (status.value, task_id),
        )

    async def advance_status(self, task_id: UUID, expected: TaskStatus, to: TaskStatus) -> None:
        """CAS lifecycle write (STEP 4): the row advances ONLY from the exact
        state the caller believes is current. Rows already beyond a race throw
        StateConflict; a lost race cannot fake durable SUCCESS."""
        if to.value == expected.value:
            return  # idempotent no-op
        rows = self._client.execute(
            "UPDATE nomadicos.tasks SET status = %s, updated_at = now() "
            "WHERE task_id = %s AND status = %s RETURNING task_id",
            (to.value, task_id, expected.value),
        )
        if not rows:
            raise StateConflict(
                f"cannot advance task {task_id} {expected.value} -> {to.value}: "
                "stored state differs (concurrent transition or stale writer)",
                context={"expected": expected.value, "attempted": to.value},
            )

    async def reconcile_interrupted(self) -> list[str]:
        """Crash reconciliation (STEP 5): every row left mid-flight by a dead
        runtime becomes FAILED — never silently successful. BLOCKED rows are
        owned-by-decision and stay untouched."""
        placeholders = ", ".join(["%s"] * len(INTERRUPT_STATUSES))
        rows = self._client.execute(
            f"UPDATE nomadicos.tasks SET status = 'FAILED', updated_at = now() "
            f"WHERE status IN ({placeholders}) RETURNING task_id",
            tuple(s.value for s in sorted(INTERRUPT_STATUSES)),
        )
        return [str(r["task_id"]) for r in rows]

    async def start_run(self, task_id: UUID, run_id: UUID | None = None) -> UUID:
        rid = run_id or uuid4()
        self._client.execute(
            "INSERT INTO nomadicos.task_runs (run_id, task_id) VALUES (%s, %s)",
            (rid, task_id),
        )
        return rid

    async def finish_run(self, run_id: UUID, result: dict[str, Any]) -> None:
        self._client.execute(
            "UPDATE nomadicos.task_runs SET finished_at = now(), status = %s, result = %s "
            "WHERE run_id = %s",
            ("FINISHED", json.dumps(result), run_id),
        )


class ModelRepository:
    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def upsert(self, record: dict[str, Any]) -> None:
        payload = {
            "model_id": record.get("model_id"),
            "display_name": record.get("display_name"),
            "path": record.get("path"),
            "format": record.get("format", "gguf"),
            "status": record.get("status", "discovered"),
            "checksum_sha256": record.get("checksum_sha256"),
            "source": record.get("source"),
            "version": record.get("version"),
            "capabilities": json.dumps(record.get("capabilities", {})),
            "resources": json.dumps(record.get("resources", {})),
        }
        self._client.execute(
            """
            INSERT INTO nomadicos.models
                (model_id, display_name, path, format, status, capabilities,
                 resources, checksum_sha256, source, version)
            VALUES (%(model_id)s, %(display_name)s, %(path)s, %(format)s, %(status)s,
                    %(capabilities)s, %(resources)s, %(checksum_sha256)s, %(source)s,
                    %(version)s)
            ON CONFLICT (model_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                path = EXCLUDED.path,
                status = EXCLUDED.status,
                capabilities = EXCLUDED.capabilities,
                resources = EXCLUDED.resources,
                updated_at = now()
            """,
            payload,
        )

    async def list_enabled(self) -> list[dict[str, Any]]:
        return self._client.execute(
            "SELECT * FROM nomadicos.models WHERE status IN ('enabled','benchmarked') "
            "ORDER BY model_id"
        )

    async def set_status(self, model_id: str, status: str) -> None:
        allowed = {
            "discovered",
            "validated",
            "benchmarked",
            "enabled",
            "disabled",
            "quarantined",
            "deprecated",
        }
        if status not in allowed:
            raise ValidationError(f"unknown model status {status}")
        self._client.execute(
            "UPDATE nomadicos.models SET status = %s, updated_at = now() WHERE model_id = %s",
            (status, model_id),
        )

    async def record_performance(
        self,
        model_id: str,
        task_family: str,
        *,
        success: bool,
        latency_ms: float | None = None,
        project_context: str | None = None,
    ) -> None:
        self._client.execute(
            MODEL_PERFORMANCE_UPSERT,
            (
                model_id,
                task_family,
                project_context or "",
                1 if success else 0,
                0 if success else 1,
                latency_ms,
                1 if success else 0,
                0 if success else 1,
                latency_ms,
                latency_ms,
                latency_ms,
            ),
        )


MODEL_PERFORMANCE_UPSERT = """
INSERT INTO nomadicos.model_performance
    (model_id, task_family, project_context, attempt_count,
     success_count, failure_count, avg_latency_ms)
VALUES (%s, %s, %s, 1, %s, %s, %s)
ON CONFLICT (model_id, task_family, project_context) DO UPDATE SET
    attempt_count = nomadicos.model_performance.attempt_count + 1,
    success_count = nomadicos.model_performance.success_count + %s,
    failure_count = nomadicos.model_performance.failure_count + %s,
    avg_latency_ms = CASE
        WHEN %s IS NULL THEN nomadicos.model_performance.avg_latency_ms
        WHEN nomadicos.model_performance.avg_latency_ms IS NULL THEN %s
        ELSE nomadicos.model_performance.avg_latency_ms * 0.9 + %s * 0.1
    END,
    last_updated = now()
"""


class AuditRepository:
    """Append-only audit persistence (BP §41-42, ADR-0020). No update/delete API."""

    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def append(self, event: AuditEvent) -> None:
        self._client.execute(
            """
            INSERT INTO nomadicos.audit_events
                (event_id, category, severity, user_id, session_id, task_id,
                 run_id, step_id, subject, decision, reason, fields)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.event_id,
                event.category.value,
                event.severity.value,
                event.user_id,
                event.session_id,
                event.task_id,
                event.run_id,
                event.step_id,
                event.subject,
                event.decision,
                event.reason,
                json.dumps(event.fields),
            ),
        )

    async def query(
        self,
        *,
        category: AuditEventCategory | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        sql = "SELECT * FROM nomadicos.audit_events WHERE 1=1"
        params: list[Any] = []
        if category is not None:
            sql += " AND category = %s"
            params.append(category.value)
        if task_id is not None:
            sql += " AND task_id::text = %s"
            params.append(task_id)
        sql += " ORDER BY occurred_at LIMIT %s"
        params.append(limit)
        rows = self._client.execute(sql, tuple(params))
        return [
            AuditEvent(
                category=AuditEventCategory(row["category"]),
                severity=row["severity"],
                event_id=row["event_id"],
                timestamp=row["occurred_at"],
                user_id=row["user_id"],
                session_id=row["session_id"],
                task_id=row["task_id"],
                run_id=row["run_id"],
                step_id=row["step_id"],
                subject=row["subject"],
                decision=row["decision"],
                reason=row["reason"],
                fields=row["fields"],
            )
            for row in rows
        ]


__all__ = ["AuditRepository", "ModelRepository", "SessionRepository", "TaskRepository"]
