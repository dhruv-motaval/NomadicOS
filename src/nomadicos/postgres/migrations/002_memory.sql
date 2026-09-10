-- NomadicOS v0.1 — Migration 002: memory engine (BP §16, §112-113, §395, §404)

CREATE TABLE IF NOT EXISTS nomadicos.memories (
    memory_id         UUID PRIMARY KEY,
    scope             TEXT NOT NULL CHECK (scope IN ('session','task','project','user','system')),
    content           TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    source            TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
    verified          BOOLEAN NOT NULL DEFAULT false,
    tags              JSONB NOT NULL DEFAULT '[]'::jsonb,
    session_id        UUID REFERENCES nomadicos.sessions(session_id),
    task_id           UUID REFERENCES nomadicos.tasks(task_id),
    project_id        TEXT,
    sensitivity       TEXT NOT NULL DEFAULT 'internal'
                      CHECK (sensitivity IN ('public','internal','sensitive')),
    embedding_model   TEXT,
    embedding_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON nomadicos.memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_project ON nomadicos.memories(project_id);
CREATE INDEX IF NOT EXISTS idx_memories_session ON nomadicos.memories(session_id);

-- BP §404: derivation tracking for controlled deletion/rollback.
CREATE TABLE IF NOT EXISTS nomadicos.memory_derivations (
    memory_id                 UUID NOT NULL REFERENCES nomadicos.memories(memory_id) ON DELETE CASCADE,
    derived_from_session_id   UUID,
    derived_from_task_id      UUID,
    derived_from_experience_id UUID,
    PRIMARY KEY (memory_id, derived_from_session_id, derived_from_task_id, derived_from_experience_id)
);
