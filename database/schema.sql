-- ============================================================
-- OpenWorker — database/schema.sql
-- Postgres + pgvector schema. Mounted into openworker-db's
-- /docker-entrypoint-initdb.d so it runs automatically on the
-- container's first startup (docker-compose.yml).
--
-- Storage split (see docs/CLAUDE_EXTENSION.md Decision 1):
--   Worker specs, connector config, skill files -> Git files, NOT here.
--   Task state, audit log, approvals, trust scores, episodic memory,
--   semantic memory (RAG) -> here, in Postgres/pgvector.
--   Working memory, n8n job queue -> Redis, NOT here.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Task state machine (Decision 2) ─────────────────────────
-- One row per task. Updated in place as the task moves through
-- its lifecycle so a crashed runtime can resume from the last
-- known state.
CREATE TABLE IF NOT EXISTS tasks (
    task_id       UUID PRIMARY KEY,
    worker_id     TEXT NOT NULL,
    worker_name   TEXT NOT NULL,
    task_input    TEXT NOT NULL,
    state         TEXT NOT NULL,
    current_tool  TEXT,
    approval_id   UUID,
    output        TEXT,
    error         TEXT,
    cost_usd      DECIMAL(10, 6),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_worker_state ON tasks (worker_id, state);

-- ── Audit log (Layer 5) — append-only, no updates, no deletes ─
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id      UUID PRIMARY KEY,
    task_id       UUID,
    worker_id     TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    tool_name     TEXT,
    decision      TEXT,
    reason        TEXT,
    payload_hash  TEXT,
    cost_usd      DECIMAL(10, 6),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_worker ON audit_log (worker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_task ON audit_log (task_id);

-- ── Episodic memory — past task summaries per worker ────────
-- embedding lets a future MemoryManager.load_semantic() also search
-- past task summaries, not just the knowledge_base, if desired.
CREATE TABLE IF NOT EXISTS worker_memory (
    memory_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    worker_id     TEXT NOT NULL,
    task_id       UUID,
    summary       TEXT NOT NULL,
    embedding     vector(1536),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_worker_memory_worker ON worker_memory (worker_id, created_at DESC);

-- ── Knowledge base (semantic memory / RAG) ──────────────────
-- worker_id NULL = available to every worker in the org.
CREATE TABLE IF NOT EXISTS knowledge_base (
    chunk_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id        TEXT NOT NULL,
    worker_id     TEXT,
    doc_name      TEXT NOT NULL,
    chunk_text    TEXT NOT NULL,
    embedding     vector(1536),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_base_org ON knowledge_base (org_id);
CREATE INDEX ON knowledge_base USING ivfflat (embedding vector_cosine_ops);

-- ── Tool access requests (Decision 5) ───────────────────────
-- A worker asking for tools it doesn't currently have. Never
-- auto-grants — a manager must explicitly approve before the
-- worker spec is edited (by a human, outside this table).
CREATE TABLE IF NOT EXISTS tool_access_requests (
    request_id        UUID PRIMARY KEY,
    worker_id         TEXT NOT NULL,
    worker_name       TEXT NOT NULL,
    tools_requested    TEXT[] NOT NULL,
    justification      TEXT NOT NULL,
    task_context        TEXT,
    suggested_tier      TEXT,
    status              TEXT DEFAULT 'pending',
    approved_tools      TEXT[],
    rejected_tools      TEXT[],
    manager_note         TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    resolved_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tool_access_requests_worker ON tool_access_requests (worker_id, status);
