"""PostgreSQL-backed experience store (BP §18, §95, §206)."""

from nomadicos.core.logging import get_logger
from nomadicos.experience.recorder import Outcome, TaskExperience
from nomadicos.experience.store import ExperienceStore
from nomadicos.postgres.client import PostgresClient

logger = get_logger("postgres.experience")


class PostgresExperienceStore(ExperienceStore):
    """Persists experiences into nomadicos.experiences (append + keyword search)."""

    def __init__(self, client: PostgresClient) -> None:
        self._client = client

    async def append(self, experience: TaskExperience) -> None:
        import json

        self._client.execute(
            """
            INSERT INTO nomadicos.experiences
                (experience_id, task_id, session_id, outcome, summary,
                 failure_class, quality_score, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                experience.experience_id,
                experience.task_id,
                experience.session_id,
                experience.outcome.value,
                experience.summary,
                experience.failure_class,
                experience.quality_score,
                json.dumps(experience.evidence),
            ),
        )
        logger.info("experience persisted id=%s", experience.experience_id)

    async def search(
        self, query: str, *, limit: int = 5, outcome: Outcome | None = None
    ) -> list[TaskExperience]:
        """Keyword ILIKE search; quality-ranked (BP §318)."""
        keywords = [w for w in query.split() if len(w) > 2][:6]
        if not keywords:
            return []
        clauses = " OR ".join(["summary ILIKE %s"] * len(keywords))
        sql = f"""
            SELECT * FROM nomadicos.experiences
            WHERE ({clauses})
            {"AND outcome = %s" if outcome else ""}
            ORDER BY quality_score DESC, created_at DESC
            LIMIT %s
        """
        params = [f"%{kw}%" for kw in keywords]
        if outcome:
            params.append(outcome.value)
        params.append(str(limit))
        rows = self._client.execute(sql, tuple(params))
        return [self._to_experience(row) for row in rows]

    async def all(self) -> list[TaskExperience]:
        rows = self._client.execute(
            "SELECT * FROM nomadicos.experiences ORDER BY created_at DESC LIMIT 500"
        )
        return [self._to_experience(row) for row in rows]

    @staticmethod
    def _to_experience(row: dict) -> TaskExperience:
        import json
        from uuid import UUID

        return TaskExperience(
            experience_id=UUID(str(row["experience_id"])),
            task_id=str(row["task_id"]) if row["task_id"] else None,
            session_id=str(row["session_id"]) if row["session_id"] else None,
            created_at=row["created_at"],
            outcome=Outcome(row["outcome"]),
            summary=row["summary"],
            failure_class=row["failure_class"],
            quality_score=row["quality_score"],
            evidence=row["fields"] if "fields" in row else json.loads(
                row["evidence"] if isinstance(row["evidence"], str) else json.dumps(row["evidence"])
            ),
        )


__all__ = ["PostgresExperienceStore"]
