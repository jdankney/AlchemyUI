-- AlchemyUI schema. SQLite, WAL mode. Applied idempotently at startup.
-- All timestamps are REAL Unix seconds, UTC.
--
-- v2: multi-user. Every owned row carries user_id; users are keyed on the
-- OIDC sub. The metering columns on jobs are for visibility, never billing:
-- nothing is ever charged for or blocked on them.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- Accounts. Created on first sight of oauth2-proxy's headers and keyed on the
-- OIDC `sub`: emails can change, `sub` cannot. Lifted from Llamacracy.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    oidc_sub        TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    is_admin        INTEGER NOT NULL DEFAULT 0,   -- cached from ADMIN_EMAILS; auth still checks config
    disabled        INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_active_at  REAL
);

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
    user_id           INTEGER REFERENCES users(id),   -- NULL only for v1 rows not yet claimed
    picked_at         REAL,                   -- worker took the job: GPU occupancy starts
    occupancy_seconds REAL,                   -- picked_at -> finished_at; the metering basis
    gpu_watts_mean    REAL,                   -- measured nvidia-smi mean while it held the GPU
    energy_wh         REAL,                   -- (gpu + NON_GPU_LOAD_WATTS) x occupancy
    rate_used         REAL,                   -- $/kWh in effect when it ran; frozen
    cost_usd          REAL,                   -- frozen at finalisation, never recomputed
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
    user_id      INTEGER REFERENCES users(id),
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    filename     TEXT NOT NULL,              -- ComfyUI's own filename, for provenance
    path         TEXT NOT NULL,              -- app-owned copy under OUTPUT_DIR
    width        INTEGER,
    height       INTEGER,
    seed         INTEGER,
    workflow_id  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    favorite     INTEGER NOT NULL DEFAULT 0,
    tags         TEXT NOT NULL DEFAULT '',   -- comma-separated, lowercase, for filtering
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_images_created ON images(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_images_favorite ON images(favorite, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_images_job ON images(job_id);

-- Uploaded source images and their masks (Phase 2: img2img, inpaint,
-- outpaint). On disk under UPLOAD_DIR, never base64-in-DB. `comfy_name` is
-- what the file is called inside ComfyUI's own input/ directory -- that is the
-- string a LoadImage node takes, and the only reason we keep it.
CREATE TABLE IF NOT EXISTS uploads (
    id           TEXT PRIMARY KEY,       -- uuid hex, also the on-disk stem
    user_id      INTEGER REFERENCES users(id),
    path         TEXT NOT NULL,          -- our normalised copy
    comfy_name   TEXT NOT NULL,          -- filename inside ComfyUI's input/
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    mask_path    TEXT,                   -- our copy of the mask, if one was drawn
    mask_comfy_name TEXT,                -- the mask inside ComfyUI's input/
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uploads_created ON uploads(created_at DESC);

-- Reference faces for likeness generation (Phase 4). Separate from `uploads`
-- because these are a set, not a single source: several are batched into one
-- job, every one is validated and normalised to the same square size on
-- arrival, and they are reused across jobs rather than belonging to one.
CREATE TABLE IF NOT EXISTS refs (
    id           TEXT PRIMARY KEY,       -- uuid hex, also the on-disk stem
    user_id      INTEGER REFERENCES users(id),
    path         TEXT NOT NULL,          -- our normalised square copy
    comfy_name   TEXT NOT NULL,          -- filename inside ComfyUI's input/
    label        TEXT NOT NULL DEFAULT '',
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    faces_found  INTEGER NOT NULL DEFAULT 1,   -- more than one means we took the largest
    face_score   REAL,                          -- detector confidence, for the UI
    face_px      INTEGER,                       -- how big the face was in the original
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_refs_created ON refs(created_at DESC);

-- Saved presets: a named workflow + parameter bundle. Stores whatever the
-- composer had, so loading one restores the whole setup rather than a prompt.
CREATE TABLE IF NOT EXISTS presets (
    id           TEXT PRIMARY KEY,
    user_id      INTEGER REFERENCES users(id),
    name         TEXT NOT NULL,
    workflow_id  TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE (user_id, name)                  -- two friends may both have a "portrait"
);

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
