-- NomadicOS v0.1 — Migration 001: core state (BP §19, §206, §378)
-- Applies: task/agent/model/audit/config/session schemas per the canonical
-- data model (BP §19) with session_id correlation (ADR-0026/F5).

CREATE SCHEMA IF NOT EXISTS nomadicos;

-- Sessions (BP §378): context containers, not memory boundaries.
CREATE TABLE IF NOT EXISTS nomadicos.sessions (
    session_id        UUID PRIMARY KEY,
    user_id           TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','paused','completed','interrupted','archived')),
    title             TEXT,
    project_id        TEXT,
    parent_session_id UUID REFERENCES nomadicos.sessions(session_id),
    isolation_mode    TEXT NOT NULL DEFAULT 'shared_memory'
                      CHECK (isolation_mode IN ('shared_memory','project_scoped','isolated','private_session')),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Tasks (BP §6.3 + F5: session_id correlation).
CREATE TABLE IF NOT EXISTS nomadicos.tasks (
    task_id           UUID PRIMARY KEY,
    session_id        UUID REFERENCES nomadicos.sessions(session_id),
    user_id           TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status            TEXT NOT NULL DEFAULT 'PLANNED'
                      CHECK (status IN ('PLANNED','RUNNING','WAITING','VERIFYING','RECOVERING',
                                        'SUCCESS','FAILED','CANCELLED','PARTIALLY_COMPLETED')),
    priority          TEXT NOT NULL DEFAULT 'NORMAL'
                      CHECK (priority IN ('CRITICAL','HIGH','NORMAL','LOW','BACKGROUND')),
    goal              TEXT NOT NULL,
    plan_version      INTEGER NOT NULL DEFAULT 0,
    state             JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Task runs (BP §206).
CREATE TABLE IF NOT EXISTS nomadicos.task_runs (
    run_id            UUID PRIMARY KEY,
    task_id           UUID NOT NULL REFERENCES nomadicos.tasks(task_id),
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'RUNNING',
    result            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON nomadicos.task_runs(task_id);

-- Models (BP §148, §276, ADR-0005/0006).
CREATE TABLE IF NOT EXISTS nomadicos.models (
    model_id          TEXT PRIMARY KEY,
    display_name      TEXT,
    path              TEXT,
    format            TEXT NOT NULL DEFAULT 'gguf',
    status            TEXT NOT NULL DEFAULT 'discovered'
                      CHECK (status IN ('discovered','validated','benchmarked','enabled',
                                        'disabled','quarantined','deprecated')),
    capabilities      JSONB NOT NULL DEFAULT '{}'::jsonb,
    resources         JSONB NOT NULL DEFAULT '{}'::jsonb,
    checksum_sha256   TEXT,
    source            TEXT,
    version           TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model performance across sessions (BP §148, §386-387).
CREATE TABLE IF NOT EXISTS nomadicos.model_performance (
    model_id          TEXT NOT NULL REFERENCES nomadicos.models(model_id),
    task_family       TEXT NOT NULL,
    project_context   TEXT NOT NULL DEFAULT '',
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    success_count     INTEGER NOT NULL DEFAULT 0,
    failure_count     INTEGER NOT NULL DEFAULT 0,
    quality_score     REAL NOT NULL DEFAULT 0,
    avg_latency_ms    REAL,
    last_updated      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, task_family, project_context)
);

-- Audit (BP §41-42, ADR-0020: append-only; no UPDATE/DELETE grants).
CREATE TABLE IF NOT EXISTS nomadicos.audit_events (
    event_id          UUID PRIMARY KEY,
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL DEFAULT 'info'
                      CHECK (severity IN ('info','warning','critical')),
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id           TEXT,
    session_id        UUID,
    task_id           UUID,
    run_id            UUID,
    step_id           TEXT,
    subject           TEXT,
    decision          TEXT,
    reason            TEXT,
    fields            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_category ON nomadicos.audit_events(category, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_task ON nomadicos.audit_events(task_id);

-- Audit immutability at the database level (BP §41-42, I7).
CREATE OR REPLACE FUNCTION nomadicos.forbid_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'nomadicos.audit_events is append-only (BP §41-42)';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_events_no_mutation ON nomadicos.audit_events;
CREATE TRIGGER audit_events_no_mutation
    BEFORE UPDATE OR DELETE ON nomadicos.audit_events
    FOR EACH ROW EXECUTE FUNCTION nomadicos.forbid_audit_mutation();

-- Experiences (BP §18, §95, §206).
CREATE TABLE IF NOT EXISTS nomadicos.experiences (
    experience_id     UUID PRIMARY KEY,
    task_id           UUID REFERENCES nomadicos.tasks(task_id),
    session_id        UUID REFERENCES nomadicos.sessions(session_id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome           TEXT NOT NULL CHECK (outcome IN ('success','failure','partial')),
    summary           TEXT NOT NULL,
    failure_class     TEXT CHECK (failure_class IN ('MODEL_FAILURE','TOOL_FAILURE','VISION_FAILURE',
                        'NETWORK_FAILURE','PERMISSION_FAILURE','RESOURCE_FAILURE','ENVIRONMENT_FAILURE',
                        'PLANNING_FAILURE','VERIFICATION_FAILURE','UNKNOWN_FAILURE')),
    quality_score     REAL NOT NULL DEFAULT 0,
    evidence          JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_experiences_task ON nomadicos.experiences(task_id);

-- Configuration registry (BP §56, §102: versioned configuration).
CREATE TABLE IF NOT EXISTS nomadicos.configuration (
    key               TEXT PRIMARY KEY,
    value             JSONB NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by        TEXT NOT NULL DEFAULT 'system'
);

-- Improvement versions (BP §44, §29-31).
CREATE TABLE IF NOT EXISTS nomadicos.improvement_versions (
    version_id        UUID PRIMARY KEY,
    parent_version    UUID REFERENCES nomadicos.improvement_versions(version_id),
    kind              TEXT NOT NULL,
    reason            TEXT NOT NULL,
    benchmark_results JSONB NOT NULL DEFAULT '{}'::jsonb,
    rollback_target   UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    active            BOOLEAN NOT NULL DEFAULT false
);
