-- NomadicOS v0.1 — Migration 003: audit correlation IDs as TEXT (BP §254).
-- Audit correlation IDs are trace identifiers (request/task/run/step strings),
-- not necessarily FK references to persisted tasks.

ALTER TABLE nomadicos.audit_events
    ALTER COLUMN session_id TYPE TEXT USING session_id::text;
ALTER TABLE nomadicos.audit_events
    ALTER COLUMN task_id TYPE TEXT USING task_id::text;
ALTER TABLE nomadicos.audit_events
    ALTER COLUMN run_id TYPE TEXT USING run_id::text;
