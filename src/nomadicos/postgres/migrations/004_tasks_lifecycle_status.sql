-- 004 (STEP 4): unified task lifecycle — tasks.status is the authoritative
-- persisted mirror of core.lifecycle.TaskState. Default becomes CREATED (the
-- true creation state); the CHECK covers the full legal vocabulary.
-- Historical reports remain untouched; prior rows keep their values.

ALTER TABLE nomadicos.tasks
    ALTER COLUMN status SET DEFAULT 'CREATED';

ALTER TABLE nomadicos.tasks
    DROP CONSTRAINT IF EXISTS tasks_status_check;

ALTER TABLE nomadicos.tasks
    ADD CONSTRAINT tasks_status_check CHECK (status IN (
        'CREATED', 'PLANNED', 'AUTHORIZED', 'RUNNING', 'WAITING', 'VERIFYING',
        'RECOVERING', 'BLOCKED', 'SUCCESS', 'FAILED', 'PARTIALLY_COMPLETED',
        'CANCELLED'
    ));
