-- AlchemyUI schema. SQLite, WAL mode. Applied idempotently at startup.
-- All timestamps are REAL Unix seconds, UTC.
--
-- Single user by design: there is no users table and no per-user column
-- anywhere. Nothing here is billed or metered -- the timing fields exist so
-- the queue can give an honest ETA, not so anyone can be charged for them.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,       -- uuid4, canonical hyphenated: also the ComfyUI prompt_id
    workflow_id       TEXT NOT NULL,          -- key into workflows/registry.json
    status            TEXT NOT NULL,          -- queued|running|completed|failed|cancelled
    prompt_id         TEXT,                   -- what ComfyUI acknowledged (== id; kept explicit for clarity)
    queue_position    INTEGER,                -- position at last broadcast; 0 = running
    params_json       TEXT NOT NULL,          -- the full resolved parameter set, as submitted
    input_image_path  TEXT,                   -- Phase 2 (img2img / inpaint source)
    mask_path         TEXT,                   -- Phase 2 (inpaint mask)
    error_text        TEXT,                   -- friendly message; error_detail holds the traceback
    error_detail      TEXT,                   -- verbatim ComfyUI node_errors / execution_error payload
    checkpoint        TEXT,                   -- resolved at submit; drives the swap-cost estimate
    steps             INTEGER,                -- denormalised from params for the ETA maths
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at DESC);

-- One row per output image. params_json is stored again here rather than only
-- on the job: that is what makes "reuse these settings", "vary this seed" and
-- a reproducibility trail possible. Cheap now, expensive to retrofit.
CREATE TABLE IF NOT EXISTS images (
    id           TEXT PRIMARY KEY,           -- uuid hex, also the on-disk filename stem
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    filename     TEXT NOT NULL,              -- ComfyUI's own filename, for provenance
    path         TEXT NOT NULL,              -- app-owned copy under OUTPUT_DIR
    width        INTEGER,
    height       INTEGER,
    seed         INTEGER,
    workflow_id  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    favorite     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_images_created ON images(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_images_job ON images(job_id);

-- Learned per-workflow durations: EWMA over observed runs, seeded from the
-- registry's `est` block. Two separate figures, because a checkpoint swap
-- (30s+) and the sampling itself scale with completely different things.
CREATE TABLE IF NOT EXISTS workflow_stats (
    workflow_id             TEXT PRIMARY KEY,
    ewma_seconds_per_step   REAL NOT NULL,
    ewma_load_seconds       REAL NOT NULL,
    samples                 INTEGER NOT NULL DEFAULT 0,
    updated_at              REAL NOT NULL
);

-- Small key/value bag for state that must survive a restart but isn't worth a
-- table: currently just the checkpoint we believe ComfyUI has resident.
-- ComfyUI has no /running endpoint to ask, so this is tracked from the last
-- completed job and presented as a hint, never as fact.
CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta(key, value) VALUES ('version', '1')
    ON CONFLICT(key) DO NOTHING;
