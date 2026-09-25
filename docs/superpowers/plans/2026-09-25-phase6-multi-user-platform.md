# Phase 6 — Multi-user Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-user v1 app into Llamacracy-style multi-user:
- accounts through Dex and oauth2-proxy;
- every row and event scoped to its owner;
- strict FIFO across users;
- per-job energy and electricity cost;
- a live admin page;
- a corrected model catalogue that includes the SD1.5 inpainting checkpoints.

The v1 UI keeps working throughout.

**Architecture:** Identity is lifted from Llamacracy (`users` keyed on OIDC `sub`, `ADMIN_EMAILS`, the 503 refusal). Every table gains `user_id` through additive, idempotent migrations. The queue carries an owner per job. Owners filter SSE fan-out, `_pick_next` may reorder only inside the head owner's run, and each job is metered from an `nvidia-smi` power sampler into columns that are frozen at finalisation. The admin UI is a separate page (`/admin`) polling `/api/admin/*`, so it survives Phase 7's SPA rewrite.

**Tech Stack:** FastAPI, Python 3.14, SQLite (WAL, one connection, one asyncio lock), vanilla JS with no build step, pytest with `asyncio_mode = "auto"`, and node (for the executed front-end boot test only).

**Spec:** [`ALCHEMYUI_SPEC.md`](../../../ALCHEMYUI_SPEC.md), §8–§11 and §14 (Phase 6). The reasoning is in [`docs/DECISIONS.md`](../../DECISIONS.md) → *v2 — the redesign*.

## Global Constraints

- Python `>=3.14`, managed with `uv`; no new runtime dependencies.
- One SQLite connection behind one asyncio lock; epoch-float timestamps (`db.now()`).
- **Never expose port 8188.**
- Users are keyed on OIDC `sub`, never on email. `ADMIN_EMAILS` only *names* admins.
- No identity headers and `DEV_MODE` unset → HTTP 503.
- **Metering never blocks a job.** `rate_used` and `cost_usd` are frozen on the job row and never recomputed.
- The admin API returns **metadata only**: never prompts, params or images of other users.
- Across users the queue order is strictly FIFO. Swap reordering happens only within the contiguous same-owner run at the head.
- Nothing machine-specific is committed. FQDNs, secrets and client ids live in gitignored files scaffolded from committed examples.
- Tests need no GPU, no ComfyUI and no LLM. The `node` test skips if node is absent.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Style: match the surrounding code: its docstring voice, comment density, and `h()`-style vanilla JS.

## File map

| File | Change | Responsibility |
|---|---|---|
| `alchemyui/schema.sql` | modify | `users` table; owner and metering columns on fresh DBs |
| `alchemyui/db.py` | modify | additive migrations, post-migration indexes, the `presets` rebuild |
| `alchemyui/config.py` | modify | `admin_emails`, metering settings, `rate_for_hour` |
| `alchemyui/identity.py` | rewrite | Llamacracy's identity; `require_admin`; the v1 row claim |
| `alchemyui/queue.py` | modify | `Job.user_id`, owner-aware cancel, fairness, per-viewer fan-out, metering |
| `alchemyui/power.py` | create | `GpuPowerSampler`, `gpu_stats()` (lifted) |
| `alchemyui/meter.py` | create | energy and cost arithmetic |
| `alchemyui/admin.py` | create | `/api/admin/*` router |
| `alchemyui/app.py` | modify | scoping on every route; `/api/me/usage`; `/admin`; mount the router |
| `alchemyui/registry.py` | modify | checkpoint `traits`, bare-name fallback |
| `workflows/registry.json` | modify | folder-path catalogue, new models, `sd15-inpaint`, `dpmpp_sde` |
| `alchemyui/cli.py` | modify | `alchemyui reassign FROM TO` |
| `alchemyui/static/admin.html`, `assets/admin.js` | create | admin page |
| `alchemyui/static/assets/app.js`, `styles.css` | modify | name and Admin link in the topbar; admin styles |
| `deploy/*` | modify / create | auth unit, oauth2-proxy cfg and env example, installer, ctl |
| `tests/conftest.py` | modify | `add_user`, `FixedSampler`, `env` marker on `live_server` |
| `tests/test_db.py`, `test_identity.py`, `test_fairness.py`, `test_scoping.py`, `test_metering.py`, `test_admin.py`, `test_catalogue.py`, `test_frontend.py` | create | per-task tests |
| `tests/js/boot.mjs` | create | node DOM shim that executes a real SPA boot |
| `.env.example`, `.gitignore`, `README.md`, `docs/DECISIONS.md` | modify | settings and docs |

Run the whole suite with `uv run pytest -q` and lint with `uv run ruff check .`.

---

### Task 1: Schema — users, owners, metering columns, migrations

**Files:**
- Modify: `alchemyui/schema.sql`
- Modify: `alchemyui/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - table `users(id, oidc_sub UNIQUE, email, display_name, is_admin, disabled, created_at, last_active_at)`;
  - `user_id INTEGER REFERENCES users(id)` (nullable) on `jobs`, `images`, `uploads`, `refs`, `presets`;
  - on `jobs`: `picked_at`, `occupancy_seconds`, `gpu_watts_mean`, `energy_wh`, `rate_used`, `cost_usd` (all `REAL`, nullable);
  - `presets` unique on `(user_id, name)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_db.py`:

```python
"""Schema migrations: a v1 database must upgrade in place, keep every row, and
end up indistinguishable from a fresh one."""

from __future__ import annotations

import sqlite3

import pytest

from alchemyui.db import Database

# The v1 shape of the tables Phase 6 changes, exactly as v1 shipped them.
V1_SCHEMA = """
CREATE TABLE jobs (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, status TEXT NOT NULL,
  prompt_id TEXT, queue_position INTEGER, params_json TEXT NOT NULL,
  input_image_path TEXT, mask_path TEXT, error_text TEXT, error_detail TEXT,
  checkpoint TEXT, steps INTEGER, created_at REAL NOT NULL, started_at REAL, finished_at REAL);
CREATE TABLE images (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
  filename TEXT NOT NULL, path TEXT NOT NULL, width INTEGER, height INTEGER, seed INTEGER,
  workflow_id TEXT NOT NULL, params_json TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE uploads (id TEXT PRIMARY KEY, path TEXT NOT NULL, comfy_name TEXT NOT NULL,
  width INTEGER NOT NULL, height INTEGER NOT NULL, mask_path TEXT, mask_comfy_name TEXT,
  created_at REAL NOT NULL);
CREATE TABLE refs (id TEXT PRIMARY KEY, path TEXT NOT NULL, comfy_name TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '', width INTEGER NOT NULL, height INTEGER NOT NULL,
  faces_found INTEGER NOT NULL DEFAULT 1, face_score REAL, face_px INTEGER,
  created_at REAL NOT NULL);
CREATE TABLE presets (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  workflow_id TEXT NOT NULL, params_json TEXT NOT NULL, created_at REAL NOT NULL,
  updated_at REAL NOT NULL);
"""

METERING = {"user_id", "picked_at", "occupancy_seconds", "gpu_watts_mean",
            "energy_wh", "rate_used", "cost_usd"}


def _cols(db: Database, table: str) -> set[str]:
    return {r["name"] for r in db._conn.execute(f"PRAGMA table_info({table})")}


def _v1_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO presets VALUES ('p1', 'portrait', 'txt2img-sdxl', '{}', 1.0, 1.0)")
    conn.execute("INSERT INTO jobs (id, workflow_id, status, params_json, created_at) "
                 "VALUES ('j1', 'txt2img-sdxl', 'completed', '{}', 1.0)")
    conn.commit()
    conn.close()


def test_fresh_db_has_owner_and_metering_columns(tmp_db_path):
    db = Database(tmp_db_path)
    assert METERING <= _cols(db, "jobs")
    for table in ("images", "uploads", "refs", "presets"):
        assert "user_id" in _cols(db, table), table
    assert {"oidc_sub", "email", "is_admin", "disabled"} <= _cols(db, "users")


def test_v1_db_migrates_in_place_and_keeps_its_rows(tmp_db_path):
    _v1_db(tmp_db_path)
    db = Database(tmp_db_path)
    assert METERING <= _cols(db, "jobs")
    assert [tuple(r) for r in db._conn.execute("SELECT name, user_id FROM presets")] == [
        ("portrait", None)]
    assert [tuple(r) for r in db._conn.execute("SELECT id FROM jobs")] == [("j1",)]
    # indexes on migrated columns exist -- they can only be created after the ALTERs
    idx = {r["name"] for r in db._conn.execute("PRAGMA index_list(jobs)")}
    assert "ix_jobs_user_finished" in idx


def test_presets_are_unique_per_user_not_globally(tmp_db_path):
    db = Database(tmp_db_path)
    for sub in ("a", "b"):
        db._conn.execute("INSERT INTO users (oidc_sub, email, created_at) VALUES (?, ?, 1)",
                         (sub, f"{sub}@x"))
    for uid in (1, 2):
        db._conn.execute("INSERT INTO presets VALUES (?, ?, 'portrait', 'w', '{}', 1, 1)",
                         (f"p{uid}", uid))
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute("INSERT INTO presets VALUES ('p3', 1, 'portrait', 'w', '{}', 1, 1)")


def test_reopening_is_idempotent(tmp_db_path):
    _v1_db(tmp_db_path)
    Database(tmp_db_path).close()
    db = Database(tmp_db_path)
    assert [tuple(r) for r in db._conn.execute("SELECT name FROM presets")] == [("portrait",)]
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_db.py -v` → FAIL (`users` table missing; no `user_id` columns).

- [ ] **Step 3: Schema.** In `alchemyui/schema.sql`:
  - Replace the header's "Single user by design…" paragraph with:
    `-- v2: multi-user. Every owned row carries user_id; users are keyed on the
    -- OIDC sub. Metering columns on jobs are for visibility, never billing.`
  - Add, immediately after the `PRAGMA` lines:

```sql
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
```

  - Add to the `jobs` definition, before `created_at`:

```sql
    user_id           INTEGER REFERENCES users(id),   -- NULL only for v1 rows not yet claimed
    picked_at         REAL,                   -- worker took the job: GPU occupancy starts
    occupancy_seconds REAL,                   -- picked_at -> finished_at; the metering basis
    gpu_watts_mean    REAL,                   -- measured nvidia-smi mean while it held the GPU
    energy_wh         REAL,                   -- (gpu + NON_GPU_LOAD_WATTS) x occupancy
    rate_used         REAL,                   -- $/kWh in effect when it ran; frozen
    cost_usd          REAL,                   -- frozen at finalisation, never recomputed
```

  - Add `user_id INTEGER REFERENCES users(id),` as the second column of `images`, `uploads` and `refs`.
  - Replace the `presets` table with:

```sql
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
```

- [ ] **Step 4: Migrations.** In `alchemyui/db.py`, replace `_MIGRATIONS` and the `__init__` body:

```python
# Additive, idempotent migrations for DBs created before a column existed.
# Fresh DBs get the column straight from schema.sql; these just catch up
# existing ones. "duplicate column name" means it's already applied.
_MIGRATIONS: list[str] = [
    "ALTER TABLE images ADD COLUMN tags TEXT NOT NULL DEFAULT ''",
    # v2 / Phase 6: ownership, and metering on the job row
    "ALTER TABLE jobs ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "ALTER TABLE jobs ADD COLUMN picked_at REAL",
    "ALTER TABLE jobs ADD COLUMN occupancy_seconds REAL",
    "ALTER TABLE jobs ADD COLUMN gpu_watts_mean REAL",
    "ALTER TABLE jobs ADD COLUMN energy_wh REAL",
    "ALTER TABLE jobs ADD COLUMN rate_used REAL",
    "ALTER TABLE jobs ADD COLUMN cost_usd REAL",
    "ALTER TABLE images ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "ALTER TABLE uploads ADD COLUMN user_id INTEGER REFERENCES users(id)",
    "ALTER TABLE refs ADD COLUMN user_id INTEGER REFERENCES users(id)",
]

# Indexes on migrated columns run AFTER the ALTERs: schema.sql is applied
# first, and an index on a column an old DB does not have yet would abort
# startup before the migration that adds it ever ran.
_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_jobs_user_finished ON jobs(user_id, finished_at)",
    "CREATE INDEX IF NOT EXISTS ix_images_user_created ON images(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_uploads_user ON uploads(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_refs_user ON refs(user_id, created_at DESC)",
]


def _rebuild_presets(conn: sqlite3.Connection) -> None:
    """v1's presets had a globally UNIQUE name. SQLite cannot drop a constraint
    in place, so an old table is rebuilt once, rows and ids intact."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(presets)")}
    if "user_id" in cols:
        return
    conn.executescript("""
        BEGIN;
        CREATE TABLE presets_v2 (
            id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            name TEXT NOT NULL, workflow_id TEXT NOT NULL, params_json TEXT NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE (user_id, name));
        INSERT INTO presets_v2 (id, user_id, name, workflow_id, params_json, created_at, updated_at)
            SELECT id, NULL, name, workflow_id, params_json, created_at, updated_at FROM presets;
        DROP TABLE presets;
        ALTER TABLE presets_v2 RENAME TO presets;
        COMMIT;
    """)
```

and in `Database.__init__`, after the `_MIGRATIONS` loop:

```python
        _rebuild_presets(self._conn)
        for stmt in _INDEXES:
            self._conn.execute(stmt)
```

- [ ] **Step 5: Run the new tests and then the full suite.** `uv run pytest tests/test_db.py -v` → PASS; `uv run pytest -q` → all pass (the existing tests never write `user_id`).

- [ ] **Step 6: Commit.** `git add alchemyui/schema.sql alchemyui/db.py tests/test_db.py && git commit -m "Phase 6: users table, owner and metering columns, in-place migration"` (with the trailer).

---

### Task 2: Identity — users, admins, disabled accounts, the v1 claim

**Files:**
- Modify: `alchemyui/config.py`
- Rewrite: `alchemyui/identity.py`
- Modify: `alchemyui/app.py` (`/api/me`)
- Modify: `tests/conftest.py` (identity header fixtures)
- Test: `tests/test_identity.py`

**Interfaces:**
- Consumes: the `users` table (Task 1).
- Produces:
  - `Principal(user_id: int, sub: str, email: str, display_name: str, is_admin: bool, disabled: bool)`;
  - `get_principal` (dependency);
  - `require_admin` (dependency → `Principal`, 403 otherwise);
  - `Settings.admin_email_set: set[str]`;
  - `tests.conftest.A`, `tests.conftest.B` header dicts.

- [ ] **Step 1: Add header fixtures to `tests/conftest.py`** (module level, after `TINY_PNG`):

```python
# Two identities as oauth2-proxy would forward them. Tests that use these run
# with DEV_MODE unset (the `env` marker), so the app trusts only the headers.
A = {"X-Forwarded-User": "sub-alice", "X-Forwarded-Email": "alice@example.com",
     "X-Forwarded-Preferred-Username": "alice"}
B = {"X-Forwarded-User": "sub-bob", "X-Forwarded-Email": "bob@example.com",
     "X-Forwarded-Preferred-Username": "bob"}


async def add_user(db, sub: str) -> int:
    """A users row for tests that drive the queue directly, below the HTTP layer."""
    return await db.insert(
        "INSERT INTO users (oidc_sub, email, display_name, created_at) VALUES (?, ?, ?, 1)",
        (sub, f"{sub}@example.com", sub))
```

- [ ] **Step 2: Write the failing tests** — `tests/test_identity.py`:

```python
"""Identity, lifted from Llamacracy: users keyed on the OIDC sub, admins named
by ADMIN_EMAILS, disabled accounts refused, and no headers means no service."""

from __future__ import annotations

import pytest

from tests.conftest import A, B

pytestmark = pytest.mark.env(DEV_MODE="", ADMIN_EMAILS="alice@example.com")


def _users(client):
    return [tuple(r) for r in client.db._conn.execute(
        "SELECT oidc_sub, email, disabled FROM users ORDER BY id")]


def test_no_identity_headers_is_refused(client):
    assert client.get("/api/me").status_code == 503


def test_first_request_creates_a_user_keyed_on_sub(client):
    me = client.get("/api/me", headers=A).json()
    assert me["display_name"] == "alice" and me["is_admin"] is True
    assert client.get("/api/me", headers=B).json()["is_admin"] is False
    assert _users(client) == [("sub-alice", "alice@example.com", 0),
                              ("sub-bob", "bob@example.com", 0)]


def test_a_changed_email_is_still_the_same_user(client):
    client.get("/api/me", headers=A)
    client.get("/api/me", headers={**A, "X-Forwarded-Email": "alice@new.example"})
    assert _users(client) == [("sub-alice", "alice@new.example", 0)]


def test_a_disabled_account_is_refused(client):
    client.get("/api/me", headers=B)
    client.db._conn.execute("UPDATE users SET disabled = 1 WHERE oidc_sub = 'sub-bob'")
    assert client.get("/api/me", headers=B).status_code == 403


def test_the_first_admin_claims_v1_rows_exactly_once(client):
    conn = client.db._conn

    def orphan(job_id):
        conn.execute("INSERT INTO jobs (id, workflow_id, status, params_json, created_at) "
                     "VALUES (?, 'txt2img-sdxl', 'completed', '{}', 1)", (job_id,))

    orphan("v1-job")
    client.get("/api/me", headers=B)                      # not an admin: claims nothing
    assert conn.execute("SELECT user_id FROM jobs").fetchone()[0] is None

    client.get("/api/me", headers=A)                      # the first admin inherits v1
    alice = conn.execute("SELECT id FROM users WHERE oidc_sub = 'sub-alice'").fetchone()[0]
    assert conn.execute("SELECT user_id FROM jobs WHERE id = 'v1-job'").fetchone()[0] == alice

    orphan("later-orphan")                                # the claim is one-shot
    client.get("/api/me", headers=A)
    assert conn.execute(
        "SELECT user_id FROM jobs WHERE id = 'later-orphan'").fetchone()[0] is None
```

- [ ] **Step 3: Run to confirm failure.** `uv run pytest tests/test_identity.py -v` → FAIL (`is_admin` missing, no `users` rows).

- [ ] **Step 4: Config.** In `alchemyui/config.py`, in the identity block:

```python
    # Comma-separated. Who may open /admin. Checked against the forwarded
    # email on every request (Llamacracy's rule); identity itself stays keyed
    # on the OIDC sub.
    admin_emails: str = ""
```

and next to `is_dev`:

```python
    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}
```

- [ ] **Step 5: Rewrite `alchemyui/identity.py`:**

```python
"""Identity comes from oauth2-proxy's forwarded headers, lifted from Llamacracy.
The app writes no auth code -- it trusts X-Forwarded-* because it only ever
listens on 127.0.0.1 with oauth2-proxy in front (see deploy/). If those headers
are absent and DEV_MODE is unset we refuse the request loudly: the app must
never serve open.

Users are keyed on the OIDC `sub` claim (X-Forwarded-User), never the email.
ADMIN_EMAILS names admins; the flag is refreshed on every request.

v1 had no owner column at all. The first admin to sign in inherits every row
v1 left behind -- once, recorded in runtime_state -- so the author's own
gallery does not vanish into an account nobody can log into.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from .config import Settings, get_settings
from .db import Database, get_db, now

log = logging.getLogger("alchemyui.identity")

_H_SUB = "x-forwarded-user"
_H_EMAIL = "x-forwarded-email"
_H_USERNAME = "x-forwarded-preferred-username"

_CLAIM_KEY = "v1_rows_claimed_by"
_OWNED_TABLES = ("jobs", "images", "uploads", "refs", "presets")


@dataclass(frozen=True)
class Principal:
    user_id: int
    sub: str
    email: str
    display_name: str
    is_admin: bool
    disabled: bool


async def _upsert_user(db: Database, sub: str, email: str, name: str,
                       is_admin: bool) -> Principal:
    row = await db.fetch_one("SELECT * FROM users WHERE oidc_sub = ?", (sub,))
    ts = now()
    shown = name or email.split("@")[0]
    if row is None:
        uid = await db.insert(
            "INSERT INTO users (oidc_sub, email, display_name, is_admin, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sub, email, shown, int(is_admin), ts, ts),
        )
        return Principal(uid, sub, email, shown, is_admin, False)

    # keep email / name / admin flag fresh; identity stays pinned to sub
    shown = name or row["display_name"] or email.split("@")[0]
    await db.execute(
        "UPDATE users SET email = ?, display_name = ?, is_admin = ?, last_active_at = ? "
        "WHERE id = ?",
        (email, shown, int(is_admin), ts, row["id"]),
    )
    return Principal(row["id"], sub, email, shown, is_admin, bool(row["disabled"]))


async def _claim_v1_rows(db: Database, principal: Principal) -> None:
    """Hand every ownerless v1 row to the first admin, exactly once."""
    if await db.fetch_one("SELECT 1 FROM runtime_state WHERE key = ?", (_CLAIM_KEY,)):
        return
    stmts = [(f"UPDATE {t} SET user_id = ? WHERE user_id IS NULL", (principal.user_id,))
             for t in _OWNED_TABLES]
    stmts.append(("INSERT INTO runtime_state (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO NOTHING", (_CLAIM_KEY, str(principal.user_id))))
    await db.transaction(stmts)
    log.warning("v1 rows with no owner were claimed by the first admin, %s (user %d)",
                principal.email, principal.user_id)


async def get_principal(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_db),
) -> Principal:
    if settings.is_dev:
        sub, email, name = settings.dev_sub, settings.dev_email.lower(), settings.dev_name
    else:
        sub = request.headers.get(_H_SUB, "").strip()
        email = request.headers.get(_H_EMAIL, "").strip().lower()
        name = request.headers.get(_H_USERNAME, "").strip()
        if not sub or not email:
            log.error(
                "request with no identity headers (X-Forwarded-User/Email) and "
                "DEV_MODE unset -- refusing. Is oauth2-proxy in front? path=%s",
                request.url.path,
            )
            raise HTTPException(
                status_code=503,
                detail="identity headers missing; the app must run behind oauth2-proxy",
            )

    is_admin = email in settings.admin_email_set
    principal = await _upsert_user(db, sub, email, name, is_admin)
    if principal.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    if principal.is_admin:
        await _claim_v1_rows(db, principal)
    return principal


async def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    return principal
```

- [ ] **Step 6: `/api/me`** in `alchemyui/app.py`:

```python
@app.get("/api/me")
async def me(principal: Principal = Depends(get_principal)):
    return {"email": principal.email, "display_name": principal.display_name,
            "is_admin": principal.is_admin}
```

- [ ] **Step 7: Run the tests.** `uv run pytest tests/test_identity.py -v` → PASS; `uv run pytest -q` → all pass.

- [ ] **Step 8: Commit.** `git commit -m "Phase 6: Llamacracy identity -- users, admins, disabled accounts, v1 claim"`

---

### Task 3: Queue ownership and cross-user fairness

**Files:**
- Modify: `alchemyui/queue.py`
- Test: `tests/test_fairness.py`, `tests/test_queue.py` (one ownership test)

**Interfaces:**
- Consumes: `jobs.user_id` (Task 1); `add_user` (Task 2).
- Produces:
  - `Job.user_id: int | None`;
  - `QueueManager.submit(workflow, params, user_id: int | None = None) -> Job`;
  - `QueueManager.cancel(job_id, user_id: int | None = None) -> bool` (a non-owner gets `False`);
  - `QueueManager.cancel_user(user_id) -> int`.

- [ ] **Step 1: Write the failing tests** — `tests/test_fairness.py`:

```python
"""Strict FIFO across users. Checkpoint-swap reordering survives, but only
inside the head owner's own run -- it must never overtake anyone else."""

from __future__ import annotations

from alchemyui.config import Settings
from alchemyui.queue import Job, QueueManager


def _qm(resident: str | None) -> QueueManager:
    qm = QueueManager(db=None, comfy=None, registry=None, settings=Settings())
    qm._resident_checkpoint = resident
    return qm


def _job(user: int | None, ckpt: str) -> Job:
    return Job(workflow_id="w", params={"checkpoint": ckpt}, user_id=user)


def _drain(qm: QueueManager) -> list[Job]:
    order = []
    while qm._pending:
        order.append(qm._pick_next())
    return order


def test_another_users_job_is_never_overtaken():
    qm = _qm("fast")
    a1, b1, a2 = _job(1, "slow"), _job(2, "fast"), _job(1, "fast")
    qm._pending = [a1, b1, a2]
    # v1 would have run a2 first (its model is loaded), jumping bob's b1
    assert _drain(qm) == [a1, b1, a2]


def test_one_users_own_jobs_are_still_grouped_by_checkpoint():
    qm = _qm("fast")
    a1, a2, b1 = _job(1, "slow"), _job(1, "fast"), _job(2, "slow")
    qm._pending = [a1, a2, b1]
    assert _drain(qm) == [a2, a1, b1]


def test_a_starved_job_cannot_jump_another_user():
    qm = _qm("fast")
    a1, b1 = _job(1, "fast"), _job(2, "slow")
    b1.skips = 99
    qm._pending = [a1, b1]
    assert _drain(qm) == [a1, b1]


def test_without_owners_it_behaves_exactly_as_v1():
    qm = _qm("fast")
    slow, fast = _job(None, "slow"), _job(None, "fast")
    qm._pending = [slow, fast]
    assert _drain(qm) == [fast, slow]
```

Append to `tests/test_queue.py` (and add `add_user` to its `from tests.conftest import …` line):

```python
@pytest.mark.asyncio
async def test_cancel_refuses_someone_elses_job(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.mode = "hang"
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        alice = await add_user(qm.db, "alice")
        bob = await add_user(qm.db, "bob")
        w = qm.reg.require("txt2img-sdxl")
        job = await qm.submit(w, w.resolve({"prompt": "x", "steps": 4}), user_id=alice)
        assert await qm.cancel(job.id, user_id=bob) is False
        assert await qm.cancel(job.id, user_id=alice) is True
        row = await qm.db.fetch_one("SELECT user_id FROM jobs WHERE id = ?", (job.id,))
        assert row["user_id"] == alice
    finally:
        await qm.stop()
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_fairness.py tests/test_queue.py -v` → FAIL (`Job` has no `user_id`).

- [ ] **Step 3: Implement in `alchemyui/queue.py`.**

(a) Replace the module docstring's last paragraph ("Checkpoint-swap-aware ordering … makes it real.") with:

```
Across users the order is strictly FIFO. Checkpoint-swap-aware ordering
(_pick_next) may only reorder a user's *own* consecutive jobs at the head of
the queue: promoting a job past someone else's would be cutting in line.
```

(b) In `Job`, after `params: dict`:

```python
    user_id: int | None = None     # owner; None only for v1 rows not yet claimed
```

(c) `submit`:

```python
    async def submit(self, workflow: WorkflowInfo, params: dict,
                     user_id: int | None = None) -> Job:
        job = Job(workflow_id=workflow.key, params=params, user_id=user_id)
        self._by_id[job.id] = job
        self._pending.append(job)
        await self._persist_job(job)
        self._wake.set()
        await self._broadcast()
        log.info("job %s queued (workflow=%s user=%s pos=%d)",
                 job.id, job.workflow_id, user_id, len(self._pending))
        return job
```

(d) `cancel`: change the signature and the first check:

```python
    async def cancel(self, job_id: str, user_id: int | None = None) -> bool:
        """Cancel a job. With `user_id`, only its owner may -- anyone else is
        told it does not exist, which is also all they are allowed to learn."""
        job = self._by_id.get(job_id)
        if job is None or job.state in TERMINAL:
            return False
        if user_id is not None and job.user_id != user_id:
            return False
```

(the rest of `cancel` stays as is), and add after it:

```python
    async def cancel_user(self, user_id: int) -> int:
        """Cancel everything a user has queued or running (a disabled account)."""
        mine = [j for j in [self._active, *self._pending] if j and j.user_id == user_id]
        n = 0
        for job in mine:
            n += await self.cancel(job.id)
        return n
```

(e) `_pick_next`: replace the body (keep the docstring's first two paragraphs and append a third):

```python
        """...(existing two paragraphs)...

        v2: all of that happens only inside the contiguous run of jobs that
        belong to the owner at the head. Across users the queue is strict
        FIFO, and a skip counter can never pull someone's job out from behind
        another user's either.
        """
        head = self._pending[0]
        run: list[Job] = []
        for job in self._pending:
            if job.user_id != head.user_id:
                break
            run.append(job)

        # Anyone in this run who has waited too long goes now, swap or no swap.
        for job in run:
            if job.skips >= self.cfg.queue_max_skips:
                self._pending.remove(job)
                return job

        resident = self._resident_checkpoint
        if resident is None or head.checkpoint == resident:
            return self._pending.pop(0)

        window = run[:max(1, self.cfg.queue_reorder_window)]
        for i, job in enumerate(window):
            if job.checkpoint == resident:
                if i == 0:
                    return self._pending.pop(0)
                for passed in window[:i]:
                    passed.skips += 1
                self._pending.remove(job)
                log.info("job %s promoted over %d of its owner's job(s): %s is already resident",
                         job.id, i, job.checkpoint)
                return job
        return self._pending.pop(0)
```

(f) `_reconcile`: in the `Job(...)` restore, add `user_id=r["user_id"],`.

(g) `_persist_job`: add `user_id` to the INSERT column list and values (right after `workflow_id`). **Not** to `DO UPDATE`: ownership never changes after insert, and the v1 claim must not be overwritten by a stale in-memory `None`.

```python
            "INSERT INTO jobs (id, workflow_id, user_id, status, prompt_id, queue_position, "
            "  params_json, input_image_path, mask_path, error_text, error_detail, checkpoint, "
            "  steps, created_at, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            ...
            (job.id, job.workflow_id, job.user_id, job.state.value, job.id, ...rest unchanged)
```

(h) `_collect`: add `user_id` to the `INSERT INTO images` (column + `job.user_id` value).

- [ ] **Step 4: Run the tests.** `uv run pytest -q` → all pass.

- [ ] **Step 5: Commit.** `git commit -m "Phase 6: job owners, owner-checked cancel, strict FIFO across users"`

---

### Task 4: Per-viewer SSE fan-out and an anonymised queue snapshot

**Files:**
- Modify: `alchemyui/queue.py`, `alchemyui/app.py` (`/api/events`, `/api/queue`)
- Modify: `tests/conftest.py` (`live_server` honours the `env` marker)
- Test: `tests/test_queue.py` (unit), `tests/test_scoping.py` (the live SSE test; that file is created here and extended in Task 5)

**Interfaces:**
- Produces:
  - `queue.EVERYONE` (sentinel);
  - `QueueManager.snapshot(viewer=EVERYONE) -> dict` — only the viewer's own jobs, with positions counted across everyone; `depth` counts everyone;
  - `QueueManager.subscribe(viewer=EVERYONE) -> asyncio.Queue`;
  - `_emit(event, owner=EVERYONE)`.

- [ ] **Step 1: `live_server` honours `env`.** In `tests/conftest.py`, give `live_server` the `request` argument and pass the marker through, exactly as `client` does:

```python
@pytest.fixture
def live_server(tmp_db_path, tmp_output_dir, monkeypatch, request):
    ...
    marker = request.node.get_closest_marker("env")
    db, comfy = _wire(monkeypatch, tmp_db_path, tmp_output_dir,
                      marker.kwargs if marker else {})
```

- [ ] **Step 2: Write the failing tests.** Append to `tests/test_queue.py`:

```python
@pytest.mark.asyncio
async def test_events_reach_only_the_jobs_owner(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        alice, bob = await add_user(qm.db, "alice"), await add_user(qm.db, "bob")
        qa, qb = qm.subscribe(alice), qm.subscribe(bob)
        w = qm.reg.require("txt2img-sdxl")
        job = await qm.submit(w, w.resolve({"prompt": "a secret fox", "steps": 4}),
                              user_id=alice)
        await asyncio.wait_for(job._done.wait(), 10)

        def drain(q):
            out = []
            while not q.empty():
                out.append(q.get_nowait())
            return out

        ea, eb = drain(qa), drain(qb)
        assert any(e["type"] == "image" for e in ea)
        assert not [e for e in eb if e["type"] in ("image", "progress", "job", "preview")]
        # bob still learns the GPU is busy -- just nothing about what's on it
        assert any(e["type"] == "queue" and e["depth"] == 1 for e in eb)
        assert "a secret fox" not in json.dumps(eb)
    finally:
        await qm.stop()
```

Create `tests/test_scoping.py`:

```python
"""Everything a user owns is served only to them: gallery, jobs, uploads,
references, presets, history -- and the event stream."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from tests.conftest import A, B

pytestmark = pytest.mark.env(DEV_MODE="")


def test_sse_never_shows_one_user_another_users_job(live_server):
    base, comfy, _ = live_server
    comfy.steps, comfy.delay = 40, 0.05
    with httpx.Client(base_url=base, timeout=30) as c:
        c.get("/api/me", headers=B)
        job_id = c.post("/api/generate", headers=A, json={
            "workflow_id": "txt2img-sdxl", "prompt": "alice's fox", "steps": 40}).json()["job_id"]
        seen, busy = [], False
        deadline = time.time() + 20
        with c.stream("GET", "/api/events", headers=B) as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    seen.append(json.loads(line[5:].strip()))
                    if seen[-1]["type"] == "queue":
                        if seen[-1]["depth"] >= 1:
                            busy = True
                        elif busy:
                            break           # alice's job came and went
                if time.time() > deadline:
                    raise AssertionError(f"stream never went idle: {seen[-3:]}")
    blob = json.dumps(seen)
    assert busy
    assert job_id not in blob and "alice's fox" not in blob
```

- [ ] **Step 3: Run to confirm failure.** `uv run pytest tests/test_queue.py::test_events_reach_only_the_jobs_owner tests/test_scoping.py -v` → FAIL.

- [ ] **Step 4: Implement in `alchemyui/queue.py`.** Add after `TERMINAL`:

```python
class _Everyone:
    """Viewer sentinel: sees every job. Used internally and by tests; no
    browser ever subscribes as it -- the admin page polls /api/admin/live."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "EVERYONE"


EVERYONE = _Everyone()
```

In `__init__`: `self._subscribers: dict[asyncio.Queue, object] = {}`.

Replace `snapshot`, `subscribe`, `unsubscribe`, `_emit` and `_broadcast`:

```python
    @staticmethod
    def _visible(job: Job, viewer: object) -> bool:
        return viewer is EVERYONE or job.user_id == viewer

    def snapshot(self, viewer: object = EVERYONE) -> dict:
        """The queue as one viewer may see it: their own jobs in full, with
        positions counted across everyone ("2 ahead of you" stays true), and
        nothing at all about anyone else's beyond the depth."""
        etas = self._etas()
        jobs = []
        ahead = 0
        if self._active is not None:
            if self._visible(self._active, viewer):
                jobs.append(self._active.public(0, etas.get(self._active.id)))
            ahead = 1
        for i, j in enumerate(self._pending):
            if self._visible(j, viewer):
                jobs.append(j.public(i + ahead, etas.get(j.id)))
        return {
            "type": "queue",
            "jobs": jobs,
            "depth": len(self._pending) + (1 if self._active else 0),
            "resident_checkpoint": self._resident_checkpoint,
            "comfy_connected": self._comfy_connected,
            "ts": now(),
        }

    def subscribe(self, viewer: object = EVERYONE) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[q] = viewer
        q.put_nowait(self.snapshot(viewer))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def _put(self, q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # A subscriber this far behind is a dead connection; the
            # browser will reconnect and recover state from /api/jobs.
            self._subscribers.pop(q, None)

    async def _emit(self, event: dict, owner: object = EVERYONE) -> None:
        """Send to everyone entitled to it: a job's events go to its owner only."""
        for q, viewer in list(self._subscribers.items()):
            if owner is EVERYONE or viewer is EVERYONE or viewer == owner:
                self._put(q, event)

    async def _broadcast(self) -> None:
        snaps: dict[object, dict] = {}
        for q, viewer in list(self._subscribers.items()):
            if viewer not in snaps:
                snaps[viewer] = self.snapshot(viewer)
            self._put(q, snaps[viewer])
```

Then add `owner=job.user_id` to **every** job-scoped `_emit` call:
- `cancel` → `{"type": "job", …}`;
- `_worker`, both `{"type": "job", …}` emits;
- `_collect` → `{"type": "image", …}`;
- `_handle_ws` → `{"type": "preview", …}` and `{"type": "progress", …}`.

For example:

```python
            await self._emit({"type": "job", "job": job.public(0)}, owner=job.user_id)
```

- [ ] **Step 5: Routes.** In `alchemyui/app.py`: `/api/events` → `q = qm.subscribe(principal.user_id)`; `/api/queue` → `return qm.snapshot(principal.user_id)`. **And** `generate` → `job = await qm.submit(workflow, p, principal.user_id)` — without it, jobs are created ownerless and their own owner's scoped stream hides them (three v1 SSE tests catch this).

- [ ] **Step 6: Run the tests.** `uv run pytest -q` → all pass (the v1 SSE tests still pass: the dev user owns their own jobs).

- [ ] **Step 7: Commit.** `git commit -m "Phase 6: per-owner event fan-out and an anonymised queue snapshot"`

---

### Task 5: Scope every route to its owner

**Files:**
- Modify: `alchemyui/app.py`
- Test: `tests/test_scoping.py` (extend)

**Interfaces:**
- Consumes: `Principal.user_id`; `qm.submit(..., user_id)`; `qm.cancel(..., user_id)`.
- Produces: `_record_upload(db, comfy, settings, data, user_id) -> dict`. Every read or write of `jobs`, `images`, `uploads`, `refs` and `presets` in `app.py` is filtered by `user_id = principal.user_id`. **A row that belongs to someone else is a 404, never a 403**: its existence is not theirs to learn.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_scoping.py`:

```python
import io
import uuid

from PIL import Image

from tests.conftest import wait_for


def _png(size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _uid(client, sub):
    return client.db._conn.execute("SELECT id FROM users WHERE oidc_sub = ?", (sub,)).fetchone()[0]


def _generate(client, who, prompt="a fox", **extra):
    r = client.post("/api/generate", headers=who,
                    json={"workflow_id": "txt2img-sdxl", "prompt": prompt, "steps": 4, **extra})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def _image_of(client, who, job_id):
    return wait_for(client, lambda: next(
        (i for i in client.get("/api/images", headers=who).json()["images"]
         if i["job_id"] == job_id), None))


def test_the_gallery_is_per_user(client):
    img = _image_of(client, A, _generate(client, A, prompt="alice's fox"))
    client.patch(f"/api/images/{img['id']}", headers=A, json={"tags": ["mine"]})
    assert client.get("/api/images", headers=B).json()["images"] == []
    assert client.get("/api/tags", headers=B).json()["tags"] == []
    assert client.get("/api/history", headers=B).json()["history"] == []
    for method, url in [("GET", f"/api/images/{img['id']}/file"),
                        ("DELETE", f"/api/images/{img['id']}"),
                        ("POST", f"/api/images/{img['id']}/reuse")]:
        assert client.request(method, url, headers=B).status_code == 404, url
    assert client.patch(f"/api/images/{img['id']}", headers=B,
                        json={"favorite": True}).status_code == 404
    assert client.get(f"/api/images/{img['id']}/file", headers=A).status_code == 200


def test_jobs_cancel_and_the_queue_are_per_user(client):
    client.comfy.mode = "hang"
    job = _generate(client, A)
    assert client.get("/api/jobs", headers=B).json()["jobs"] == []
    snap = client.get("/api/queue", headers=B).json()
    assert snap["depth"] == 1 and snap["jobs"] == []
    assert client.get(f"/api/jobs/{job}/preview", headers=B).status_code == 404
    assert client.post(f"/api/jobs/{job}/cancel", headers=B).status_code == 404
    assert client.post(f"/api/jobs/{job}/cancel", headers=A).status_code == 200


def test_uploads_are_per_user(client):
    up = client.post("/api/uploads", headers=A,
                     files={"file": ("a.png", _png(), "image/png")}).json()
    assert client.get(f"/api/uploads/{up['id']}/file", headers=B).status_code == 404
    assert client.post(f"/api/uploads/{up['id']}/mask", headers=B,
                       files={"file": ("m.png", _png(), "image/png")}).status_code == 404
    r = client.post("/api/generate", headers=B, json={
        "workflow_id": "img2img-sdxl", "prompt": "x", "image_id": up["id"]})
    assert r.status_code == 404


def test_references_are_per_user(client):
    client.get("/api/me", headers=A)
    ref_id = uuid.uuid4().hex
    client.db._conn.execute(
        "INSERT INTO refs (id, user_id, path, comfy_name, width, height, created_at) "
        "VALUES (?, ?, '/nonexistent.png', 'alchemyui/ref.png', 768, 768, 1)",
        (ref_id, _uid(client, "sub-alice")))
    assert len(client.get("/api/references", headers=A).json()["references"]) == 1
    assert client.get("/api/references", headers=B).json()["references"] == []
    assert client.get(f"/api/references/{ref_id}/file", headers=B).status_code == 404
    assert client.delete(f"/api/references/{ref_id}", headers=B).status_code == 404
    r = client.post("/api/generate", headers=B, json={
        "workflow_id": "likeness-sdxl", "prompt": "x", "reference_ids": [ref_id]})
    assert r.status_code == 404


def test_presets_are_per_user(client):
    body = {"name": "portrait", "workflow_id": "txt2img-sdxl", "params": {"prompt": "p"}}
    pa = client.post("/api/presets", headers=A, json=body).json()
    pb = client.post("/api/presets", headers=B, json=body).json()
    assert pa["id"] != pb["id"]                              # same name, two presets
    assert [p["id"] for p in client.get("/api/presets", headers=B).json()["presets"]] == [pb["id"]]
    assert client.delete(f"/api/presets/{pa['id']}", headers=B).status_code == 404
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_scoping.py -v` → FAIL (B sees A's rows).

- [ ] **Step 3: Implement in `alchemyui/app.py`.** Apply exactly these edits:

1. `_record_upload(db, comfy, settings, data, user_id: int)` inserts `user_id`:

```python
    await db.execute(
        "INSERT INTO uploads (id, user_id, path, comfy_name, width, height, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (upload_id, user_id, path, comfy_name, width, height, now()),
    )
```

   Its callers `create_upload` and `reuse_image` pass `principal.user_id`.

2. Every single-row lookup gains `AND user_id = ?` with `principal.user_id`:
   - `attach_mask`: `SELECT * FROM uploads WHERE id = ? AND user_id = ?`
   - `reuse_image`: `SELECT path FROM images WHERE id = ? AND user_id = ?`
   - `upload_file`: `SELECT path, mask_path FROM uploads WHERE id = ? AND user_id = ?`
   - `reference_file`, `delete_reference`: `... FROM refs WHERE id = ? AND user_id = ?`
   - `generate`: the uploads lookup and each refs lookup
   - `update_image` (both SELECTs), `image_file`, `delete_image`: `... FROM images WHERE id = ? AND user_id = ?`
   - `delete_preset`: `SELECT id FROM presets WHERE id = ? AND user_id = ?`

3. Lists:
   - `list_references`: `SELECT * FROM refs WHERE user_id = ? ORDER BY created_at DESC LIMIT 100`
   - `list_jobs`: `SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?`
   - `list_images`: start `where, args = ["user_id = ?"], [principal.user_id]`, and always emit ` WHERE ` (the list is never empty now).
   - `list_tags`: `SELECT tags FROM images WHERE user_id = ? AND tags != ''`
   - `prompt_history`: `... FROM images WHERE user_id = ? GROUP BY ...`
   - `list_presets`: `SELECT * FROM presets WHERE user_id = ? ORDER BY name`

4. `create_reference` inserts `user_id` (add the column and `principal.user_id`).

5. `save_preset`: `existing = ... WHERE user_id = ? AND name = ?`, and the INSERT gains `user_id`. Replace "there is one user" in its comment with "and presets are per user".

6. `generate`: already passes `principal.user_id` to `qm.submit` (moved into Task 4).

7. `cancel_job`: `if not await qm.cancel(job_id, principal.user_id):`.

8. `job_preview`: `if job is None or job.user_id != principal.user_id or job.preview is None:` → 404.

9. In the module docstring, add: "Every row belongs to one user and is served only to them; someone else's is a 404, not a 403."

- [ ] **Step 4: Run the tests.** `uv run pytest -q` → all pass.

- [ ] **Step 5: Commit.** `git commit -m "Phase 6: every route scoped to its owner"`

---

### Task 6: The model catalogue — folder paths, new SD1.5 models, inpaint family

**Files:**
- Modify: `workflows/registry.json`, `alchemyui/registry.py`
- Modify: `tests/test_platform.py`, `tests/test_api.py`, `tests/test_queue.py` (old bare names)
- Test: `tests/test_catalogue.py`

**Interfaces:**
- Produces:
  - `CheckpointInfo.traits: tuple[str, ...]`, one of `fast | quality | photoreal | inpaint`;
  - `Registry.checkpoint(name)`, which accepts a v1 bare filename and returns the folder-pathed entry;
  - family `sd15-inpaint`.

- [ ] **Step 1: Write the failing tests** — `tests/test_catalogue.py`:

```python
"""The shipped model catalogue matches what is on disk: checkpoints named by
their family folder (as ComfyUI lists them), inpainting on dedicated
inpainting UNets, and fast few-step defaults for the Hyper variants."""

from __future__ import annotations

from pathlib import Path

from alchemyui.registry import load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
FOLDER = {"sdxl": "SDXL/", "sd15": "SD1.5/", "sd15-inpaint": "SD1.5-Inpaint/"}


def test_every_checkpoint_is_named_by_its_family_folder():
    for c in REG.all_checkpoints():
        assert c.name.startswith(FOLDER[c.family]), c.name


def test_every_checkpoint_has_known_traits():
    for c in REG.all_checkpoints():
        assert set(c.traits) <= {"fast", "quality", "photoreal", "inpaint"}, c.name
        assert ("inpaint" in c.traits) == (c.family == "sd15-inpaint"), c.name


def test_inpaint_and_outpaint_default_to_the_inpainting_unet():
    for key in ("inpaint-sdxl", "outpaint-sdxl"):
        w = REG.require(key)
        assert REG.checkpoint(w.checkpoint).family == "sd15-inpaint", key


def test_generation_cannot_pick_an_inpainting_unet():
    w = REG.require("txt2img-sdxl")
    assert all(c.family != "sd15-inpaint" for c in REG.checkpoints_for(w))


def test_hyper_checkpoints_sample_in_a_few_steps():
    hyper = [c for c in REG.all_checkpoints() if "Hyper" in c.name]
    assert len(hyper) == 2
    for c in hyper:
        assert c.defaults["steps"] <= 8 and c.defaults["cfg"] <= 2.5
        assert c.defaults["sampler"] == "dpmpp_sde"


def test_a_v1_bare_filename_still_resolves():
    c = REG.checkpoint("epicrealism_naturalSinRC1VAE.safetensors")
    assert c is not None and c.name == "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
    w = REG.require("txt2img-sdxl")
    assert w.resolve({"prompt": "x"}, c)["checkpoint"] == c.name
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_catalogue.py -v` → FAIL.

- [ ] **Step 3: Registry code.** In `alchemyui/registry.py`:
  - Add `KNOWN_TRAITS = frozenset({"fast", "quality", "photoreal", "inpaint"})` near the `_KINDS` constant.
  - Add `traits: tuple[str, ...] = ()` to `CheckpointInfo` (after `family`), and `"traits": list(self.traits),` to its `public()`.
  - In `load_registry`'s checkpoint loop:

```python
        traits = tuple(c.get("traits") or ())
        unknown = set(traits) - KNOWN_TRAITS
        if unknown:
            raise RegistryError(f"checkpoint {name}: unknown trait(s) {sorted(unknown)}")
```

    and pass `traits=traits` to `CheckpointInfo(...)`.
  - Replace `Registry.checkpoint`:

```python
    def checkpoint(self, name: str) -> CheckpointInfo | None:
        hit = self._checkpoints.get(name)
        if hit is None and name and "/" not in name:
            # v1 named checkpoints by bare filename; the catalogue now names
            # them by family folder, as ComfyUI lists them. Presets and
            # gallery params saved by v1 still say the bare name.
            hit = next((c for c in self._checkpoints.values()
                        if c.name.rsplit("/", 1)[-1] == name), None)
        return hit
```

- [ ] **Step 4: Rewrite the catalogue.** Run this one-off script, then review the diff:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

p = Path("workflows/registry.json")
reg = json.loads(p.read_text())
SD15_ASPECTS = {"1:1": [512, 512], "3:4": [576, 768], "4:3": [768, 576],
                "9:16": [512, 896], "16:9": [896, 512]}
SDXL_ASPECTS = reg["checkpoints"]["RealVisXL_V5.0_fp16.safetensors"]["aspects"]
HYPER = {"steps": 6, "cfg": 1.5, "sampler": "dpmpp_sde", "scheduler": "karras"}

reg["checkpoints"] = {
    "SDXL/RealVisXL_V5.0_fp16.safetensors": {
        "label": "RealVis XL", "family": "sdxl", "traits": ["quality", "photoreal"],
        "blurb": "Photorealistic, 1024px native. Slower, best quality.",
        "aspects": SDXL_ASPECTS, "defaults": {"steps": 25, "cfg": 6.5},
        "est": {"seconds_per_step": 5.25, "load_s": 35}},
    "SD1.5/realisticVisionV60B1_v51HyperVAE_pruned_fp16.safetensors": {
        "label": "Realistic Vision Hyper", "family": "sd15", "traits": ["fast", "photoreal"],
        "blurb": "Photorealistic SD1.5 in six steps. The fastest good model here.",
        "aspects": SD15_ASPECTS, "defaults": HYPER,
        "est": {"seconds_per_step": 2.4, "load_s": 15}},
    "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors": {
        "label": "Realistic Vision 6.0", "family": "sd15", "traits": ["photoreal"],
        "blurb": "Photorealistic SD1.5, full step count.",
        "aspects": SD15_ASPECTS, "defaults": {"steps": 25, "cfg": 5.0},
        "est": {"seconds_per_step": 1.2, "load_s": 15}},
    "SD1.5/epicrealism_naturalSinRC1VAE.safetensors": {
        "label": "epiCRealism", "family": "sd15", "traits": ["photoreal"],
        "blurb": "Photorealistic SD1.5. About four times faster than SDXL.",
        "aspects": SD15_ASPECTS, "defaults": {"steps": 25, "cfg": 7.0},
        "est": {"seconds_per_step": 1.2, "load_s": 15}},
    "SD1.5/v1-5-pruned-emaonly.safetensors": {
        "label": "SD 1.5 base", "family": "sd15", "traits": [],
        "blurb": "The stock SD1.5 weights. Mostly useful as a baseline.",
        "aspects": SD15_ASPECTS, "defaults": {"steps": 25, "cfg": 7.5},
        "est": {"seconds_per_step": 1.1, "load_s": 15}},
    "SD1.5-Inpaint/realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16.safetensors": {
        "label": "Realistic Vision 6.0 Inpainting", "family": "sd15-inpaint",
        "traits": ["inpaint", "photoreal"],
        "blurb": "A dedicated inpainting UNet: repaints without a seam.",
        "aspects": SD15_ASPECTS, "defaults": {"steps": 25, "cfg": 5.0},
        "est": {"seconds_per_step": 1.2, "load_s": 15}},
    "SD1.5-Inpaint/realisticVisionV60B1_v51HyperInpaintVAE_pruned_fp16.safetensors": {
        "label": "Realistic Vision Hyper Inpainting", "family": "sd15-inpaint",
        "traits": ["inpaint", "fast"],
        "blurb": "The inpainting UNet in six steps.",
        "aspects": SD15_ASPECTS, "defaults": HYPER,
        "est": {"seconds_per_step": 2.4, "load_s": 15}},
}
INPAINT_DEFAULT = "SD1.5-Inpaint/realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16.safetensors"
for key, w in reg["workflows"].items():
    if key in ("inpaint-sdxl", "outpaint-sdxl"):
        w["checkpoint"] = INPAINT_DEFAULT
        w["families"] = ["sd15-inpaint", "sd15", "sdxl"]
    else:
        w["checkpoint"] = "SDXL/" + w["checkpoint"]
    samplers = (w.get("choices") or {}).get("sampler")
    if samplers is not None and "dpmpp_sde" not in samplers:
        samplers.append("dpmpp_sde")        # what the Hyper models are tuned for
p.write_text(json.dumps(reg, indent=2) + "\n")
PY
git diff --stat workflows/registry.json
```

- [ ] **Step 5: Update the old names in existing tests.** In `tests/test_platform.py`, `tests/test_api.py` and `tests/test_queue.py`, replace:
  - `"RealVisXL_V5.0_fp16.safetensors"` → `"SDXL/RealVisXL_V5.0_fp16.safetensors"`
  - `"epicrealism_naturalSinRC1VAE.safetensors"` → `"SD1.5/epicrealism_naturalSinRC1VAE.safetensors"`

  Leave `not-a-model.safetensors` alone. Use `sed -i` on those three files and check the diff.

- [ ] **Step 6: Run the tests.** `uv run pytest -q` and `uv run alchemyui validate` → all pass and the registry is valid. (`validate --live` runs in Task 11 against the real ComfyUI.)

- [ ] **Step 7: Commit.** `git commit -m "Phase 6: folder-pathed catalogue, Realistic Vision + Hyper, sd15-inpaint default"`

---

### Task 7: Metering — power sampling, energy and cost frozen on the job

**Files:**
- Create: `alchemyui/power.py`, `alchemyui/meter.py`
- Modify: `alchemyui/config.py`, `alchemyui/queue.py`, `tests/conftest.py`
- Test: `tests/test_metering.py`

**Interfaces:**
- Produces:
  - `power.GpuPowerSampler` (`.start()`, `await .stop()`, `.mean -> float | None`);
  - `power.gpu_stats() -> dict`;
  - `meter.Reading(occupancy_seconds, gpu_watts_mean, energy_wh, rate_used, cost_usd)`;
  - `meter.finalise(picked_at, finished_at, gpu_watts_mean, settings) -> Reading`;
  - `Settings.rate_for_hour(h) -> float`;
  - `QueueManager(..., sampler_factory=None)`;
  - `Job` fields `picked_at`, `occupancy_seconds`, `gpu_watts_mean`, `energy_wh`, `rate_used`, `cost_usd`;
  - `tests.conftest.FixedSampler`.

- [ ] **Step 1: `FixedSampler` in `tests/conftest.py`**, and make `_wire` use it, so no test ever shells out to `nvidia-smi`:

```python
class FixedSampler:
    """Stands in for GpuPowerSampler: a known wattage, no nvidia-smi."""

    def __init__(self, watts: float | None = 40.0):
        self.watts = watts

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    @property
    def mean(self) -> float | None:
        return self.watts
```

In `_wire`, add `monkeypatch.setattr(queuemod, "GpuPowerSampler", FixedSampler)`. In `tests/test_queue.py` `_make_qm`, pass `sampler_factory=FixedSampler` to `QueueManager(...)`.

- [ ] **Step 2: Write the failing tests** — `tests/test_metering.py`:

```python
"""Metering: visibility, not billing. Every job that held the GPU records its
occupancy, measured watts, energy and cost -- frozen on the row."""

from __future__ import annotations

import asyncio
import time

import pytest

from alchemyui.config import Settings
from alchemyui.meter import finalise
from tests.conftest import FakeComfy, FixedSampler, add_user
from tests.test_queue import _make_qm, _settings


def test_energy_is_gpu_plus_the_rest_of_the_machine_over_occupancy():
    s = Settings()
    object.__setattr__(s, "non_gpu_load_watts", 20.0)
    object.__setattr__(s, "electricity_rate", 0.40)
    r = finalise(1000.0, 1360.0, 70.0, s)           # 6 minutes at 70 + 20 W
    assert r.occupancy_seconds == 360.0
    assert r.energy_wh == pytest.approx(9.0)        # 90 W x 0.1 h
    assert r.rate_used == 0.40
    assert r.cost_usd == pytest.approx(0.0036)      # 0.009 kWh x $0.40


def test_missing_power_reading_falls_back_rather_than_zero():
    s = Settings()
    object.__setattr__(s, "gpu_fallback_watts", 60.0)
    object.__setattr__(s, "non_gpu_load_watts", 0.0)
    r = finalise(0.0, 3600.0, None, s)
    assert r.gpu_watts_mean is None and r.energy_wh == pytest.approx(60.0)


def test_time_of_use_rate_is_taken_from_the_local_hour_it_ran():
    s = Settings()
    table = {str(h): (0.10 if h < 12 else 0.50) for h in range(24)}
    object.__setattr__(s, "tou_schedule", __import__("json").dumps(table))
    morning = time.mktime((2026, 9, 25, 9, 0, 0, 0, 0, -1))
    evening = time.mktime((2026, 9, 25, 19, 0, 0, 0, 0, -1))
    assert finalise(morning, morning + 60, 50, s).rate_used == 0.10
    assert finalise(evening, evening + 60, 50, s).rate_used == 0.50


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "error"])
async def test_every_job_that_ran_is_metered_and_frozen(tmp_db_path, tmp_output_dir, mode):
    comfy = FakeComfy()
    comfy.mode = mode
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        uid = await add_user(qm.db, "alice")
        w = qm.reg.require("txt2img-sdxl")
        job = await qm.submit(w, w.resolve({"prompt": "x", "steps": 4}), user_id=uid)
        await asyncio.wait_for(job._done.wait(), 10)
        row = await qm.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job.id,))
        s = qm.cfg
        assert row["gpu_watts_mean"] == 40.0                      # FixedSampler
        assert row["occupancy_seconds"] > 0
        assert row["energy_wh"] == pytest.approx(
            (40.0 + s.non_gpu_load_watts) * row["occupancy_seconds"] / 3600, rel=1e-3)
        assert row["cost_usd"] == pytest.approx(row["energy_wh"] / 1000 * row["rate_used"],
                                                rel=1e-3)
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_job_cancelled_while_queued_used_nothing(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.mode = "hang"
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        w = qm.reg.require("txt2img-sdxl")
        await qm.submit(w, w.resolve({"prompt": "a", "steps": 4}))
        waiting = await qm.submit(w, w.resolve({"prompt": "b", "steps": 4}))
        await qm.cancel(waiting.id)
        row = await qm.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (waiting.id,))
        assert row["energy_wh"] is None and row["picked_at"] is None
    finally:
        await qm.stop()
```

(`_settings` is imported to keep the helper's settings shape available; drop that import if ruff flags it unused.)

- [ ] **Step 3: Run to confirm failure.** `uv run pytest tests/test_metering.py -v` → FAIL (no `alchemyui.meter`).

- [ ] **Step 4: Config.** In `alchemyui/config.py`, add `import json` and `from pydantic import field_validator`, and this block before the queue settings:

```python
    # --- metering (v2) -------------------------------------------------------
    # Visibility, not billing: every job's energy and its electricity cost are
    # recorded and shown per user. Nothing is ever blocked on them.
    # CPU and the rest of the laptop while it generates. Intel RAPL is
    # root-only here (the Platypus mitigation), so this is an estimate.
    non_gpu_load_watts: float = 25.0
    # Used only if nvidia-smi gave no reading for a job.
    gpu_fallback_watts: float = 60.0
    electricity_rate: float = 0.30           # $/kWh, flat
    tou_schedule: str = ""                   # JSON {"0": 0.25, ..., "23": 0.30}, local hours

    @property
    def tou_table(self) -> dict[int, float] | None:
        if not self.tou_schedule.strip():
            return None
        raw = json.loads(self.tou_schedule)
        return {int(k): float(v) for k, v in raw.items()}

    def rate_for_hour(self, hour_local: int) -> float:
        table = self.tou_table
        if table is None:
            return self.electricity_rate
        return table.get(hour_local % 24, self.electricity_rate)

    @field_validator("tou_schedule")
    @classmethod
    def _validate_tou(cls, v: str) -> str:
        if v.strip():
            json.loads(v)  # fail fast on malformed JSON
        return v
```

- [ ] **Step 5: `alchemyui/power.py`** (lifted from Llamacracy's `power.py`, plus `gpu_stats` from its `admin.py`):

```python
"""GPU power and state via nvidia-smi, lifted from Llamacracy. The laptop's
mobile 1660 Ti does report power.draw (checked: ~2.4W idle), but there is no
cumulative energy counter, so draw is sampled while a job holds the GPU and
the mean is multiplied by the occupancy. CPU energy is estimated in config.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("alchemyui.power")

_HAVE_NVIDIA_SMI = shutil.which("nvidia-smi") is not None


async def _query(fields: str) -> list[str] | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return [x.strip() for x in out.decode().strip().splitlines()[0].split(",")]
    except (TimeoutError, OSError, ValueError, IndexError):
        return None


class GpuPowerSampler:
    """Background poller. Start it when a job takes the GPU, stop it at the
    end, then read `.mean` (None if nvidia-smi was unavailable)."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._samples: list[float] = []

    async def _loop(self) -> None:
        while True:
            row = await _query("power.draw")
            try:
                if row:
                    self._samples.append(float(row[0]))
            except ValueError:
                pass            # "[N/A]" on a GPU that can't report it
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if _HAVE_NVIDIA_SMI and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="gpu-power-sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def mean(self) -> float | None:
        if not self._samples:
            return None
        return round(sum(self._samples) / len(self._samples), 1)


async def gpu_stats() -> dict:
    """One snapshot for the admin page's live tiles."""
    row = await _query("memory.used,memory.total,temperature.gpu,power.draw,utilization.gpu")
    try:
        used, total, temp, power, util = row  # type: ignore[misc]
        return {"vram_used_mib": int(float(used)), "vram_total_mib": int(float(total)),
                "temp_c": float(temp), "power_w": float(power), "util_pct": float(util),
                "available": True}
    except (TypeError, ValueError):
        return {"available": False}
```

- [ ] **Step 6: `alchemyui/meter.py`:**

```python
"""Metering arithmetic, kept pure so it can be tested to the cent.

    energy_wh = (gpu_watts_mean + NON_GPU_LOAD_WATTS) x occupancy_seconds / 3600
    cost_usd  = energy_wh / 1000 x rate          (rate: TOU hour, or flat)

Occupancy runs from the worker taking the job to its end, model load
included; waiting in the queue is never counted. Everything that actually
held the GPU is attributed to its owner, failures and cancellations too --
Llamacracy zeroes those because it bills, and AlchemyUI only tracks. The
result is frozen on the job row and never recomputed, so a rate change
cannot rewrite history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class Reading:
    occupancy_seconds: float
    gpu_watts_mean: float | None
    energy_wh: float
    rate_used: float
    cost_usd: float


def finalise(picked_at: float, finished_at: float, gpu_watts_mean: float | None,
             settings: Settings) -> Reading:
    occupancy = max(0.0, finished_at - picked_at)
    gpu = gpu_watts_mean if gpu_watts_mean is not None else settings.gpu_fallback_watts
    energy = (gpu + settings.non_gpu_load_watts) * occupancy / 3600
    rate = settings.rate_for_hour(time.localtime(picked_at).tm_hour)
    return Reading(
        occupancy_seconds=round(occupancy, 3),
        gpu_watts_mean=gpu_watts_mean,
        energy_wh=round(energy, 6),
        rate_used=rate,
        cost_usd=round(energy / 1000 * rate, 8),
    )
```

- [ ] **Step 7: Queue integration** in `alchemyui/queue.py`:
  - Imports: `from .meter import finalise` and `from .power import GpuPowerSampler`.
  - `Job` fields, after `finished_at`:

```python
    picked_at: float | None = None          # worker took it: GPU occupancy starts
    occupancy_seconds: float | None = None
    gpu_watts_mean: float | None = None
    energy_wh: float | None = None
    rate_used: float | None = None
    cost_usd: float | None = None
```

  - `__init__`: add the parameter `sampler_factory=None` and `self._sampler_factory = sampler_factory`.
  - `_worker`, right after `self._active = job`:

```python
            job.picked_at = now()
            # Looked up at call time so tests can swap the module's sampler.
            sampler = (self._sampler_factory or GpuPowerSampler)()
            sampler.start()
```

    and in `finally`, before `self._active = None`:

```python
                await sampler.stop()
```

    and inside `if not shutting_down:`, after `job.finished_at` is set and before `_persist_job`:

```python
                    r = finalise(job.picked_at, job.finished_at, sampler.mean, self.cfg)
                    job.occupancy_seconds, job.gpu_watts_mean = r.occupancy_seconds, r.gpu_watts_mean
                    job.energy_wh, job.rate_used, job.cost_usd = r.energy_wh, r.rate_used, r.cost_usd
```

  - `_persist_job`: add `picked_at, occupancy_seconds, gpu_watts_mean, energy_wh, rate_used, cost_usd` to both the INSERT column/value lists **and** the `DO UPDATE SET` list (`picked_at=excluded.picked_at, …`).

- [ ] **Step 8: Run the tests.** `uv run pytest -q` → all pass.

- [ ] **Step 9: Commit.** `git commit -m "Phase 6: per-job GPU power sampling, energy and cost frozen on the row"`

---

### Task 8: The admin API, and a user's own usage

**Files:**
- Create: `alchemyui/admin.py`
- Modify: `alchemyui/queue.py` (`Job.meta`, `QueueManager.admin_view`), `alchemyui/app.py`
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `require_admin`, `qm.cancel_user`, `power.gpu_stats`, and the metering columns.
- Produces:
  - `GET /api/admin/live`;
  - `GET /api/admin/users?days=` (0 = all time);
  - `GET /api/admin/failures?limit=`;
  - `POST /api/admin/users/{id}/disabled {disabled}`;
  - `GET /api/me/usage`.

- [ ] **Step 1: Write the failing tests** — `tests/test_admin.py`:

```python
"""The admin API: who ran what, for how long, at what cost -- metadata only,
never anyone's prompts or images."""

from __future__ import annotations

import json

import pytest

from tests.conftest import A, B, wait_for

pytestmark = pytest.mark.env(DEV_MODE="", ADMIN_EMAILS="alice@example.com")


def _run(client, who, prompt):
    job = client.post("/api/generate", headers=who, json={
        "workflow_id": "txt2img-sdxl", "prompt": prompt, "steps": 4}).json()["job_id"]
    wait_for(client, lambda: any(j["id"] == job and j["status"] == "completed"
                                 for j in client.get("/api/jobs", headers=who).json()["jobs"]))
    return job


def test_admin_routes_are_admin_only(client):
    client.get("/api/me", headers=A)
    for url in ("/api/admin/live", "/api/admin/users", "/api/admin/failures", "/admin"):
        assert client.get(url, headers=B).status_code == 403, url
        assert client.get(url, headers=A).status_code == 200, url


def test_live_view_names_owners_but_never_shows_prompts(client):
    client.comfy.mode = "hang"
    client.get("/api/me", headers=A)
    client.post("/api/generate", headers=B, json={
        "workflow_id": "txt2img-sdxl", "prompt": "bob's private prompt", "steps": 4})
    live = client.get("/api/admin/live", headers=A).json()
    assert live["queue"]["jobs"][0]["owner"] == "bob"
    assert "bob's private prompt" not in json.dumps(live)
    assert "gpu" in live and "resident_checkpoint" in live


def test_usage_is_totalled_per_user(client):
    _run(client, A, "one")
    _run(client, B, "two")
    _run(client, B, "three")
    users = {u["display_name"]: u for u in
             client.get("/api/admin/users?days=0", headers=A).json()["users"]}
    assert users["alice"]["jobs"] == 1 and users["bob"]["jobs"] == 2
    assert users["bob"]["kwh"] > 0 and users["bob"]["cost_usd"] > 0
    mine = client.get("/api/me/usage", headers=B).json()
    assert mine["images"] == 2 and mine["kwh"] == pytest.approx(users["bob"]["kwh"])


def test_failures_list_errors_without_their_payloads(client):
    client.comfy.mode = "error"
    client.post("/api/generate", headers=B, json={
        "workflow_id": "txt2img-sdxl", "prompt": "secret", "steps": 4})
    fails = wait_for(client, lambda: client.get(
        "/api/admin/failures", headers=A).json()["failures"])
    assert fails[0]["owner"] == "bob" and "KSampler" in fails[0]["error_text"]
    assert "secret" not in json.dumps(fails) and "traceback" not in json.dumps(fails)


def test_disabling_a_user_cancels_their_work_and_locks_them_out(client):
    client.comfy.mode = "hang"
    client.get("/api/me", headers=A)
    job = client.post("/api/generate", headers=B, json={
        "workflow_id": "txt2img-sdxl", "prompt": "x", "steps": 4}).json()["job_id"]
    bob = client.db._conn.execute("SELECT id FROM users WHERE oidc_sub = 'sub-bob'").fetchone()[0]
    r = client.post(f"/api/admin/users/{bob}/disabled", headers=A, json={"disabled": True})
    assert r.json()["cancelled"] == 1
    assert client.get("/api/me", headers=B).status_code == 403
    status = client.db._conn.execute("SELECT status FROM jobs WHERE id = ?", (job,)).fetchone()[0]
    assert status in ("cancelled", "running")      # running ones are interrupted, then cancelled


def test_an_admin_cannot_disable_themselves(client):
    client.get("/api/me", headers=A)
    alice = client.db._conn.execute(
        "SELECT id FROM users WHERE oidc_sub = 'sub-alice'").fetchone()[0]
    r = client.post(f"/api/admin/users/{alice}/disabled", headers=A, json={"disabled": True})
    assert r.status_code == 400
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_admin.py -v` → FAIL (404s).

- [ ] **Step 3: Queue views.** In `alchemyui/queue.py`, add to `Job`:

```python
    def meta(self, position: int | None, eta_s: float | None, owner: str) -> dict:
        """What the admin page may see: no prompt, no params, no images."""
        return {
            "id": self.id, "owner": owner, "workflow_id": self.workflow_id,
            "checkpoint": self.checkpoint, "status": self.state.value,
            "position": position, "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "overall": round(self.overall, 4), "picked_at": self.picked_at,
            "started_at": self.started_at,
        }
```

and to `QueueManager`:

```python
    def admin_view(self, names: dict[int, str]) -> dict:
        etas = self._etas()
        rows, ahead = [], 0
        if self._active is not None:
            a = self._active
            rows.append(a.meta(0, etas.get(a.id), names.get(a.user_id, "unclaimed")))
            ahead = 1
        for i, j in enumerate(self._pending):
            rows.append(j.meta(i + ahead, etas.get(j.id), names.get(j.user_id, "unclaimed")))
        return {"jobs": rows, "depth": len(rows)}
```

- [ ] **Step 4: `alchemyui/admin.py`:**

```python
"""Admin API. Every route is behind require_admin, which checks the forwarded
email against ADMIN_EMAILS on each request (Llamacracy's rule).

Metadata only, by design: who ran what, for how long, at what cost. Nothing
here returns another user's prompts, parameters or images -- the person with
the GPU should not be able to read their friends' prompts or see their face
edits, and likeness photos are what make that line matter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .db import Database, get_db, now
from .identity import Principal, require_admin
from .power import gpu_stats
from .queue import QueueManager, get_queue

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


async def _names(db: Database) -> dict[int, str]:
    rows = await db.fetch_all("SELECT id, email, display_name FROM users")
    return {r["id"]: r["display_name"] or r["email"] for r in rows}


@router.get("/live")
async def live(qm: QueueManager = Depends(get_queue), db: Database = Depends(get_db)):
    """Polled by the admin page every couple of seconds, which is also what
    keeps nvidia-smi from being run when nobody is looking."""
    return {
        "gpu": await gpu_stats(),
        "queue": qm.admin_view(await _names(db)),
        "resident_checkpoint": qm._resident_checkpoint,
        "comfy_connected": qm._comfy_connected,
        "ts": now(),
    }


@router.get("/users")
async def users(days: int = 30, db: Database = Depends(get_db)):
    since = now() - days * 86400 if days > 0 else 0.0
    rows = await db.fetch_all(
        "SELECT u.id, u.email, u.display_name, u.is_admin, u.disabled, u.last_active_at, "
        "  COUNT(j.id) AS jobs, COALESCE(SUM(j.occupancy_seconds), 0) AS occ, "
        "  COALESCE(SUM(j.energy_wh), 0) AS wh, COALESCE(SUM(j.cost_usd), 0) AS cost "
        "FROM users u LEFT JOIN jobs j ON j.user_id = u.id "
        "  AND j.energy_wh IS NOT NULL AND j.finished_at >= ? "
        "GROUP BY u.id ORDER BY cost DESC, u.id",
        (since,),
    )
    return {"days": days, "users": [{
        "id": r["id"], "email": r["email"], "display_name": r["display_name"],
        "is_admin": bool(r["is_admin"]), "disabled": bool(r["disabled"]),
        "last_active_at": r["last_active_at"], "jobs": r["jobs"],
        "gpu_minutes": round(r["occ"] / 60, 2), "kwh": round(r["wh"] / 1000, 5),
        "cost_usd": round(r["cost"], 4),
    } for r in rows]}


@router.get("/failures")
async def failures(limit: int = 20, db: Database = Depends(get_db)):
    """The friendly message only -- error_detail can carry node inputs, and
    node inputs are prompts."""
    rows = await db.fetch_all(
        "SELECT j.id, j.workflow_id, j.error_text, j.finished_at, u.display_name, u.email "
        "FROM jobs j LEFT JOIN users u ON u.id = j.user_id "
        "WHERE j.status = 'failed' ORDER BY j.finished_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    )
    return {"failures": [{
        "id": r["id"], "workflow_id": r["workflow_id"], "error_text": r["error_text"],
        "finished_at": r["finished_at"],
        "owner": r["display_name"] or r["email"] or "unclaimed",
    } for r in rows]}


class DisabledFlag(BaseModel):
    disabled: bool


@router.post("/users/{user_id}/disabled")
async def set_disabled(user_id: int, body: DisabledFlag,
                       principal: Principal = Depends(require_admin),
                       db: Database = Depends(get_db),
                       qm: QueueManager = Depends(get_queue)):
    if user_id == principal.user_id and body.disabled:
        raise HTTPException(400, "you cannot disable your own account")
    if await db.fetch_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise HTTPException(404, "no such user")
    await db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), user_id))
    cancelled = await qm.cancel_user(user_id) if body.disabled else 0
    return {"id": user_id, "disabled": body.disabled, "cancelled": cancelled}
```

- [ ] **Step 5: `app.py` wiring.** Add `import time`; import `require_admin`; `from . import admin as admin_api`; then `app.include_router(admin_api.router)` right after `app = FastAPI(...)`. Add:

```python
@app.get("/api/me/usage")
async def my_usage(principal: Principal = Depends(get_principal),
                   db: Database = Depends(get_db)):
    """This calendar month, local time -- the same numbers the admin sees,
    scoped to yourself."""
    lt = time.localtime()
    since = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    j = await db.fetch_one(
        "SELECT COUNT(*) AS jobs, COALESCE(SUM(energy_wh), 0) AS wh, "
        "  COALESCE(SUM(cost_usd), 0) AS cost FROM jobs "
        "WHERE user_id = ? AND energy_wh IS NOT NULL AND finished_at >= ?",
        (principal.user_id, since))
    i = await db.fetch_one("SELECT COUNT(*) AS n FROM images WHERE user_id = ? AND created_at >= ?",
                           (principal.user_id, since))
    return {"since": since, "images": i["n"], "jobs": j["jobs"],
            "kwh": round(j["wh"] / 1000, 5), "cost_usd": round(j["cost"], 4)}
```

and, in the SPA section, before `index`:

```python
    @app.get("/admin")
    async def admin_page(principal: Principal = Depends(require_admin)):
        return FileResponse(STATIC_DIR / "admin.html")
```

  Create a placeholder `alchemyui/static/admin.html` containing `<!doctype html><title>AlchemyUI admin</title><div id="app"></div>` so the route test passes. Task 9 replaces it.

- [ ] **Step 6: Run the tests.** `uv run pytest -q` → all pass.

- [ ] **Step 7: Commit.** `git commit -m "Phase 6: admin API (live, per-user usage, failures, disable) and /api/me/usage"`

---

### Task 9: The admin page, the topbar, and an executed front-end boot

**Files:**
- Create: `alchemyui/static/admin.html`, `alchemyui/static/assets/admin.js`, `tests/js/boot.mjs`, `tests/test_frontend.py`
- Modify: `alchemyui/static/assets/app.js` (topbar), `alchemyui/static/assets/styles.css`

**Interfaces:**
- Consumes: `/api/me` `is_admin`, and `/api/admin/*`.
- Produces: `node tests/js/boot.mjs <base_url> <script_path>`, which prints one JSON line `{"error": str|null, "rendered": int, "requests": [str]}`.

- [ ] **Step 1: Write the boot harness** — `tests/js/boot.mjs`. This is the DOM shim that found the v1 blank-page bug, generalised to take the server and script as arguments:

```javascript
// Executes a real SPA script against a real server, in node, with just enough
// DOM to boot. Prints one JSON line. A script that is syntactically valid but
// throws at load (v1's `const f = w =`) fails here; `node --check` never can.
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [BASE, SCRIPT] = process.argv.slice(2);
const byId = new Map();
const requests = [];
let error = null;

const ctx2d = () => new Proxy({}, { get: () => () => ({}) });
function el(tag) {
  const t = {
    nodeType: 1, tagName: String(tag).toUpperCase(), childNodes: [], style: {}, dataset: {},
    className: '', id: '', value: '', textContent: '', innerHTML: '', checked: false,
    files: [], width: 512, height: 512, disabled: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute(k, v) { if (k === 'id') { t.id = v; byId.set(v, p); } },
    getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {}, removeEventListener() {},
    append(...k) { t.childNodes.push(...k); }, appendChild(k) { t.childNodes.push(k); return k; },
    insertBefore(k) { t.childNodes.push(k); return k; },
    replaceChildren(...k) { t.childNodes = k.filter(x => x != null && x !== false); },
    remove() {}, replaceWith() {}, focus() {}, blur() {}, click() {}, scrollIntoView() {},
    setPointerCapture() {}, releasePointerCapture() {}, select() {}, setSelectionRange() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 100, height: 100, right: 100, bottom: 100, x: 0, y: 0 }),
    querySelector: () => null, querySelectorAll: () => [], closest: () => null, contains: () => false,
    getContext: () => ctx2d(), toBlob(cb) { cb(new Blob([])); },
  };
  const p = new Proxy(t, { get: (o, k) => o[k], set: (o, k, v) => (o[k] = v, true) });
  return p;
}
const APP = el('div');
globalThis.document = {
  getElementById: id => (id === 'app' ? APP : (byId.get(id) || null)),
  createElement: el, createElementNS: (_ns, tag) => el(tag),
  createTextNode: s => ({ nodeType: 3, textContent: String(s) }),
  querySelector: () => null, querySelectorAll: () => [],
  addEventListener() {}, removeEventListener() {},
  body: el('body'), documentElement: el('html'), head: el('head'),
  activeElement: null, hidden: false, visibilityState: 'visible', title: '',
};
globalThis.window = globalThis;
globalThis.location = { href: BASE + '/', origin: BASE, pathname: '/', search: '', hash: '', reload() {} };
globalThis.history = { replaceState() {}, pushState() {} };
Object.defineProperty(globalThis, 'navigator', {
  value: { maxTouchPoints: 0, userAgent: 'node', clipboard: { writeText: async () => {} } },
  configurable: true,
});
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} });
const store = new Map();
globalThis.localStorage = { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = f => setTimeout(f, 0);
globalThis.cancelAnimationFrame = () => {};
globalThis.EventSource = class { constructor(u) { requests.push(u); } addEventListener() {} close() {} };
globalThis.Image = class { set src(v) { this._s = v; } };
globalThis.FileReader = class { readAsDataURL() {} };
globalThis.URL.createObjectURL = () => 'blob:fake';
globalThis.URL.revokeObjectURL = () => {};
globalThis.scrollTo = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
const realFetch = globalThis.fetch;
globalThis.fetch = (u, o) => { requests.push(String(u)); return realFetch(String(u).startsWith('http') ? u : BASE + u, o); };

const done = () => {
  console.log(JSON.stringify({ error, rendered: APP.childNodes.length, requests }));
  process.exit(0);
};
process.on('unhandledRejection', e => { error = String(e && e.stack || e); done(); });
process.on('uncaughtException', e => { error = String(e && e.stack || e); done(); });

// Copied to .mjs so node evaluates it as a module, in strict mode -- the same
// semantics <script type="module"> gives it in a browser.
const tmp = join(tmpdir(), `alchemyui-boot-${process.pid}.mjs`);
writeFileSync(tmp, readFileSync(SCRIPT));
try { await import(pathToFileURL(tmp).href); } finally { rmSync(tmp, { force: true }); }
setTimeout(done, 3000);
```

- [ ] **Step 2: Write the failing test** — `tests/test_frontend.py`:

```python
"""The front-end is executed, not just parsed. v1 shipped `const f = w =` --
valid syntax, a ReferenceError at module load, and a blank page -- past
`node --check`. This boots each page's real script against a real server."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "alchemyui" / "static" / "assets"
NODE = shutil.which("node")

pytestmark = [pytest.mark.skipif(NODE is None, reason="node is not installed"),
              pytest.mark.env(ADMIN_EMAILS="dev@localhost")]


def _boot(base: str, script: str) -> dict:
    out = subprocess.run([NODE, str(REPO / "tests" / "js" / "boot.mjs"), base, str(ASSETS / script)],
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines, f"no output from node: {out.stderr}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("script, first_call", [("app.js", "/api/me"),
                                                ("admin.js", "/api/admin/live")])
def test_page_boots_and_renders(live_server, script, first_call):
    base, _, _ = live_server
    result = _boot(base, script)
    assert result["error"] is None, result["error"]
    assert result["rendered"] > 0
    assert first_call in result["requests"]
```

- [ ] **Step 3: Run to confirm failure.** `uv run pytest tests/test_frontend.py -v` → `app.js` PASSES (v1 is already fixed) and `admin.js` FAILS (the file doesn't exist). That is the expected red.

- [ ] **Step 4: `alchemyui/static/admin.html`:**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AlchemyUI admin</title>
<link rel="icon" href="/assets/alchemyui-favicon.svg" type="image/svg+xml">
<meta name="theme-color" content="#151a23">
<meta name="color-scheme" content="dark">
<link rel="stylesheet" href="/assets/styles.css">
</head>
<body>
<div id="app"></div>
<script type="module" src="/assets/admin.js"></script>
</body>
</html>
```

- [ ] **Step 5: `alchemyui/static/assets/admin.js`:**

```javascript
// AlchemyUI admin -- vanilla JS, no build step. A separate page rather than a
// view inside the SPA: the SPA is replaced wholesale in Phase 7, this is not.
//
// Metadata only. The API never returns anyone's prompts or images, and this
// page never asks.
const $app = document.getElementById('app');

const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
};
async function get(p) { const r = await fetch(p); if (!r.ok) throw new Error(`${r.status} ${p}`); return r.json(); }
async function post(p, b) {
  const r = await fetch(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

const S = { live: null, users: [], failures: [], days: 30, error: null };
const fmtMoney = n => '$' + (n || 0).toFixed(n >= 1 ? 2 : 4);
const fmtAgo = ts => {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago`
    : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function tile(label, value, sub) {
  return h('div', { class: 'adm-tile' }, h('div', { class: 'small faint' }, label),
    h('div', { class: 'adm-big' }, value), sub ? h('div', { class: 'small muted' }, sub) : null);
}

function liveSection() {
  const L = S.live;
  if (!L) return h('p', { class: 'muted' }, 'Loading…');
  const g = L.gpu || {};
  const running = L.queue.jobs.find(j => j.position === 0);
  return h('section', {},
    h('div', { class: 'adm-tiles' },
      tile('GPU draw', g.available ? `${g.power_w.toFixed(0)} W` : 'n/a', g.available ? `${g.temp_c.toFixed(0)} °C` : null),
      tile('Utilisation', g.available ? `${g.util_pct.toFixed(0)}%` : 'n/a'),
      tile('VRAM', g.available ? `${(g.vram_used_mib / 1024).toFixed(1)} / ${(g.vram_total_mib / 1024).toFixed(0)} GB` : 'n/a'),
      tile('Queue', String(L.queue.depth), running ? `running: ${running.owner}` : 'idle'),
      tile('ComfyUI', L.comfy_connected ? 'connected' : 'down', (L.resident_checkpoint || '').split('/').pop() || 'nothing loaded')),
    L.queue.jobs.length ? h('table', { class: 'adm-table' },
      h('tr', {}, ['#', 'Owner', 'Workflow', 'Model', 'Status', 'Progress'].map(c => h('th', {}, c))),
      L.queue.jobs.map(j => h('tr', {},
        h('td', {}, String(j.position)), h('td', {}, j.owner), h('td', {}, j.workflow_id),
        h('td', {}, (j.checkpoint || '').split('/').pop()), h('td', {}, j.status),
        h('td', {}, j.position === 0 ? `${Math.round(j.overall * 100)}%` : (j.eta_s ? `~${Math.round(j.eta_s)}s` : ''))))) : null);
}

function usersSection() {
  const pick = d => h('button', { class: 'chip' + (S.days === d ? ' on' : ''), onclick: () => { S.days = d; loadUsers(); } },
    d ? `${d} days` : 'All time');
  return h('section', {},
    h('div', { class: 'adm-head' }, h('h2', {}, 'Usage'), h('div', { class: 'row' }, pick(7), pick(30), pick(0))),
    h('table', { class: 'adm-table' },
      h('tr', {}, ['User', 'Jobs', 'GPU min', 'kWh', 'Cost', 'Last seen', ''].map(c => h('th', {}, c))),
      S.users.map(u => h('tr', { class: u.disabled ? 'faint' : '' },
        h('td', {}, u.display_name, u.is_admin ? h('span', { class: 'small muted' }, ' · admin') : null),
        h('td', {}, String(u.jobs)), h('td', {}, u.gpu_minutes.toFixed(1)),
        h('td', {}, u.kwh.toFixed(3)), h('td', {}, fmtMoney(u.cost_usd)),
        h('td', { class: 'small muted' }, fmtAgo(u.last_active_at)),
        h('td', {}, u.is_admin ? null : h('button', { class: 'chip', onclick: () => toggle(u) },
          u.disabled ? 'Enable' : 'Disable'))))),
    h('p', { class: 'small faint' },
      'Energy is the GPU\'s measured draw plus an estimate for the rest of the laptop, over the time each job held the GPU. An estimate of the GPU\'s share, not a wall-socket reading.'));
}

function failuresSection() {
  return h('section', {}, h('h2', {}, 'Recent failures'),
    S.failures.length ? h('table', { class: 'adm-table' },
      S.failures.map(f => h('tr', {}, h('td', { class: 'small muted' }, fmtAgo(f.finished_at)),
        h('td', {}, f.owner), h('td', {}, f.workflow_id), h('td', { class: 'small' }, f.error_text || ''))))
      : h('p', { class: 'muted' }, 'None.'));
}

function render() {
  $app.replaceChildren(h('div', { class: 'wrap adm' },
    h('header', { class: 'adm-head' }, h('h1', {}, 'AlchemyUI admin'), h('a', { href: '/' }, 'Back to the app')),
    S.error ? h('div', { class: 'card', style: 'padding:12px;color:var(--danger)' }, S.error) : null,
    liveSection(), usersSection(), failuresSection()));
}

async function toggle(u) {
  try { await post(`/api/admin/users/${u.id}/disabled`, { disabled: !u.disabled }); await loadUsers(); }
  catch (e) { S.error = e.message; render(); }
}
async function loadUsers() {
  try { S.users = (await get(`/api/admin/users?days=${S.days}`)).users; S.error = null; }
  catch (e) { S.error = e.message; }
  render();
}
async function poll() {
  // Only while someone is looking: this is also what keeps nvidia-smi quiet.
  if (document.visibilityState === 'visible') {
    try { S.live = await get('/api/admin/live'); S.error = null; }
    catch (e) { S.error = e.message; }
    render();
  }
  setTimeout(poll, 2000);
}

async function boot() {
  render();
  await Promise.all([loadUsers(), get('/api/admin/failures').then(r => { S.failures = r.failures; })]);
  await poll();
}
boot();
```

  Note that `poll()` is called before `loadUsers()` resolves, so move the `/api/admin/live` call first: change `boot()` to `render(); await poll(); await Promise.all([...])`, so that `/api/admin/live` is the first request (the test asserts it appears; it does not assert order, but first is the natural contract).

- [ ] **Step 6: Admin styles** — append to `alchemyui/static/assets/styles.css`:

```css
/* --- admin page ----------------------------------------------------------- */
.adm { padding-top: 20px; padding-bottom: 40px; }
.adm section { margin: 24px 0; }
.adm-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.adm-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.adm-tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; }
.adm-big { font-size: 1.5rem; font-weight: 600; margin: 2px 0; }
.adm-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.92rem; }
.adm-table th { text-align: left; font-weight: 500; color: var(--muted); padding: 6px 8px; border-bottom: 1px solid var(--border); }
.adm-table td { padding: 7px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
@media (max-width: 640px) { .adm-table { font-size: 0.82rem; } .adm-table td, .adm-table th { padding: 5px 4px; } }
```

  Before relying on these, check that `--surface`, `--border`, `--radius`, `--muted` and `--danger` exist in `styles.css`'s `:root` (`grep -n -- '--surface\|--border\|--radius\|--muted' alchemyui/static/assets/styles.css`) and substitute the file's actual token names if any differ.

- [ ] **Step 7: Topbar** in `alchemyui/static/assets/app.js`. In `topbar()`, append to the header's children, at the right-hand end:

```javascript
    h('div', { class: 'row small muted', style: 'margin-left:auto;gap:10px' },
      S.me?.display_name || '',
      S.me?.is_admin ? h('a', { href: '/admin' }, 'Admin') : null),
```

  If the topbar already has a right-aligned group, append these two items to it instead of adding a second `margin-left:auto` group.

- [ ] **Step 8: Run the tests.** `uv run pytest tests/test_frontend.py -v` then `uv run pytest -q` → all pass. Also `node --check alchemyui/static/assets/admin.js`.

- [ ] **Step 9: Commit.** `git commit -m "Phase 6: admin page, topbar identity, executed front-end boot test"`

---

### Task 10: Deploy — the auth edge, installer, ctl, config, and `reassign`

**Files:**
- Create: `deploy/oauth2-proxy.cfg`, `deploy/oauth2-proxy.env.example`, `deploy/systemd/alchemyui-auth.service`
- Modify: `deploy/install.sh`, `deploy/alchemyui-cli.sh`, `.env.example`, `.gitignore`, `alchemyui/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `alchemyui reassign FROM_EMAIL TO_EMAIL`, which moves every owned row from one account to another;
  - an `alchemyui-auth.service` user unit on `wt0:4180`.

- [ ] **Step 1: Write the failing test** — `tests/test_cli.py`:

```python
"""`alchemyui reassign`: move one account's everything to another -- the v1
rows the dev identity claimed, or a friend whose Dex account was recreated
with a new sub."""

from __future__ import annotations

from alchemyui.cli import main
from alchemyui.db import Database


def test_reassign_moves_every_owned_row(tmp_db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", tmp_db_path)
    from alchemyui.config import get_settings
    get_settings.cache_clear()
    db = Database(tmp_db_path)
    c = db._conn
    for sub in ("old", "new"):
        c.execute("INSERT INTO users (oidc_sub, email, created_at) VALUES (?, ?, 1)",
                  (sub, f"{sub}@x"))
    c.execute("INSERT INTO jobs (id, user_id, workflow_id, status, params_json, created_at) "
              "VALUES ('j', 1, 'w', 'completed', '{}', 1)")
    c.execute("INSERT INTO presets VALUES ('p', 1, 'portrait', 'w', '{}', 1, 1)")
    db.close()

    assert main(["reassign", "old@x", "new@x"]) == 0
    db = Database(tmp_db_path)
    assert db._conn.execute("SELECT user_id FROM jobs").fetchone()[0] == 2
    assert db._conn.execute("SELECT user_id FROM presets").fetchone()[0] == 2
    get_settings.cache_clear()


def test_reassign_refuses_unknown_accounts(tmp_db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", tmp_db_path)
    from alchemyui.config import get_settings
    get_settings.cache_clear()
    Database(tmp_db_path).close()
    assert main(["reassign", "nobody@x", "also-nobody@x"]) == 1
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to confirm failure.** `uv run pytest tests/test_cli.py -v` → FAIL (no `reassign` subcommand).

- [ ] **Step 3: `alchemyui/cli.py`.** Add to the module docstring:

```
    alchemyui reassign FROM TO  move every image, job, upload, reference and
                                preset from one account (by email) to another
```

  Add the command:

```python
def cmd_reassign(args: argparse.Namespace) -> int:
    """Presets are unique per user by name, so one the target already has by
    that name is left with its original owner rather than failing the move."""
    import sqlite3

    from .db import Database

    db = Database(get_settings().database_path)
    try:
        c = db._conn

        def uid(email):
            r = c.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
            return r[0] if r else None

        src, dst = uid(args.src), uid(args.dst)
        if src is None or dst is None:
            print(f"  {BAD} both accounts must have signed in at least once "
                  f"({args.src}: {'ok' if src else 'unknown'}, {args.dst}: {'ok' if dst else 'unknown'})")
            return 1
        c.execute("BEGIN")
        try:
            for t in ("jobs", "images", "uploads", "refs"):
                n = c.execute(f"UPDATE {t} SET user_id = ? WHERE user_id = ?", (dst, src)).rowcount
                print(f"  {OK} {t:<8} {n} row(s)")
            n = c.execute("UPDATE OR IGNORE presets SET user_id = ? WHERE user_id = ?",
                          (dst, src)).rowcount
            print(f"  {OK} presets  {n} row(s)")
            c.execute("COMMIT")
        except sqlite3.Error:
            c.execute("ROLLBACK")
            raise
        return 0
    finally:
        db.close()
```

  Register it in `main`:

```python
    r = sub.add_parser("reassign", help="move one account's rows to another, by email")
    r.add_argument("src", metavar="FROM")
    r.add_argument("dst", metavar="TO")
    r.set_defaults(func=cmd_reassign)
```

- [ ] **Step 4: `deploy/oauth2-proxy.cfg`** (committed; host-independent):

```
# oauth2-proxy config for AlchemyUI, adapted from Llamacracy's. Non-secret,
# host-independent settings only. Secrets and hostnames live in
# deploy/oauth2-proxy.env (gitignored): client id/secret, cookie secret, the
# Dex issuer on dex-server, this laptop's redirect URL and the upstream port.
# http_address is injected at start from wt0 by the systemd unit.

provider = "oidc"

# Llamacracy's Dex, on dex-server's NetBird interface. AlchemyUI is a second
# client of it -- register it there (see the installer's output).
scope = "openid email profile offline_access"
email_domains = ["*"]                 # trust anyone Dex authenticates
skip_provider_button = true

# Users must reach the app by the FQDN in OAUTH2_PROXY_REDIRECT_URL, not the
# raw 100.x address, or the post-login redirect lands on the wrong cookie host.

pass_access_token = false
pass_authorization_header = false
set_xauthrequest = true
pass_user_headers = true              # X-Forwarded-User (sub), -Email, -Preferred-Username
prefer_email_to_user = false          # keep X-Forwarded-User = sub

cookie_name = "_alchemyui"
cookie_secure = false                 # plain HTTP inside the WireGuard tunnel
cookie_samesite = "lax"
cookie_expire = "168h"
cookie_refresh = "1h"

reverse_proxy = false
skip_auth_routes = ["GET=^/healthz$"]
```

- [ ] **Step 5: `deploy/oauth2-proxy.env.example`:**

```
# deploy/oauth2-proxy.env.example -- deploy/install.sh copies this to
# deploy/oauth2-proxy.env (gitignored), fills in this laptop's NetBird FQDN
# and the app's port, and generates the cookie and client secrets.

# Llamacracy's Dex, on dex-server. Set ALCHEMYUI_DEX_FQDN when running the
# installer, or edit REPLACE_DEX_FQDN here by hand.
OAUTH2_PROXY_OIDC_ISSUER_URL=http://REPLACE_DEX_FQDN:5556
# This laptop, by FQDN (see `netbird status`), never the raw 100.x address.
OAUTH2_PROXY_REDIRECT_URL=http://REPLACE_FQDN:4180/oauth2/callback
OAUTH2_PROXY_UPSTREAMS=http://127.0.0.1:REPLACE_PORT

# Must match the `alchemyui` staticClient registered in Dex's config.yaml.
OAUTH2_PROXY_CLIENT_ID=alchemyui
OAUTH2_PROXY_CLIENT_SECRET=

# openssl rand -base64 32 | tr -- '+/' '-_'
OAUTH2_PROXY_COOKIE_SECRET=
```

- [ ] **Step 6: `deploy/systemd/alchemyui-auth.service`:**

```
# ~/.config/systemd/user/alchemyui-auth.service  (installed by deploy/install.sh,
# which replaces @REPO@ with the absolute path of this checkout)
# oauth2-proxy: AlchemyUI's auth edge. Binds this laptop's NetBird interface
# (wt0, discovered at start) on :4180, authenticates against Llamacracy's Dex
# on dex-server, and forwards X-Forwarded-{User,Email,Preferred-Username} to
# the app on 127.0.0.1. Adapted from Llamacracy's llamacracy-auth.service.
[Unit]
Description=AlchemyUI auth edge (oauth2-proxy on wt0)
After=alchemyui.service
Wants=alchemyui.service
ConditionPathExists=/sys/class/net/wt0

[Service]
Type=simple
WorkingDirectory=@REPO@
ExecStartPre=/bin/sh -c 'a=$(ip -4 -o addr show wt0 | awk "{print \\$4}" | cut -d/ -f1); \
  echo "OAUTH2_PROXY_HTTP_ADDRESS=$a:4180" > %t/alchemyui-auth.env'
EnvironmentFile=-%t/alchemyui-auth.env
EnvironmentFile=@REPO@/deploy/oauth2-proxy.env
ExecStart=%h/.local/bin/oauth2-proxy --config @REPO@/deploy/oauth2-proxy.cfg
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 7: `deploy/install.sh`.**
  - Update the header comment from "one systemd *user* unit" to "two systemd *user* units: the app, and its oauth2-proxy auth edge (started only once it is configured)".
  - Add `OAUTH2_PROXY_VERSION="v7.6.0"` (Llamacracy's pin) after `UNIT_DIR`.
  - After step 6 (the CLI), insert:

```bash
# --- 7. auth edge: oauth2-proxy in front, Llamacracy's Dex behind ------------
BIN="$HOME/.local/bin"
if [ ! -x "$BIN/oauth2-proxy" ]; then
  say "installing oauth2-proxy $OAUTH2_PROXY_VERSION"
  tmp="$(mktemp -d)"
  curl -sSL -o "$tmp/o.tar.gz" \
    "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/${OAUTH2_PROXY_VERSION}/oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-amd64.tar.gz"
  tar -xzf "$tmp/o.tar.gz" -C "$tmp" --strip-components=1
  install -m755 "$tmp/oauth2-proxy" "$BIN/oauth2-proxy"
  rm -rf "$tmp"
fi

AUTH_ENV="$REPO/deploy/oauth2-proxy.env"
fqdn="$(netbird status 2>/dev/null | awk '/^FQDN:/ {print $2}')"
if [ ! -f "$AUTH_ENV" ]; then
  cp "$REPO/deploy/oauth2-proxy.env.example" "$AUTH_ENV"
  [ -n "$fqdn" ] && sed -i "s#REPLACE_FQDN#${fqdn}#g" "$AUTH_ENV"
  [ -n "${ALCHEMYUI_DEX_FQDN:-}" ] && sed -i "s#REPLACE_DEX_FQDN#${ALCHEMYUI_DEX_FQDN}#g" "$AUTH_ENV"
  sed -i "s#REPLACE_PORT#${PORT}#g" "$AUTH_ENV"
  sed -i "s#^OAUTH2_PROXY_COOKIE_SECRET=.*#OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | tr -- '+/' '-_')#" "$AUTH_ENV"
  sed -i "s#^OAUTH2_PROXY_CLIENT_SECRET=.*#OAUTH2_PROXY_CLIENT_SECRET=$(openssl rand -hex 32)#" "$AUTH_ENV"
  warn "created deploy/oauth2-proxy.env with fresh secrets"
fi
sed -e "s#@REPO@#${REPO}#g" "$REPO/deploy/systemd/alchemyui-auth.service" \
  > "$UNIT_DIR/alchemyui-auth.service"
systemctl --user daemon-reload

if grep -q 'REPLACE_' "$AUTH_ENV"; then
  warn "deploy/oauth2-proxy.env still has REPLACE_ placeholders -- the auth edge stays off."
  warn "  re-run with ALCHEMYUI_DEX_FQDN=<dex-server's NetBird FQDN> ./deploy/install.sh,"
  warn "  or edit them by hand, then re-run."
else
  secret="$(grep '^OAUTH2_PROXY_CLIENT_SECRET=' "$AUTH_ENV" | cut -d= -f2-)"
  redirect="$(grep '^OAUTH2_PROXY_REDIRECT_URL=' "$AUTH_ENV" | cut -d= -f2-)"
  say "register AlchemyUI in Llamacracy's Dex (deploy/dex/config.yaml on dex-server),"
  say "under staticClients, then restart Dex there:"
  cat <<EOF

    - id: alchemyui
      name: AlchemyUI
      secret: ${secret}
      redirectURIs:
        - ${redirect}

EOF
  if [ "${ALCHEMYUI_START_AUTH:-0}" = "1" ]; then
    systemctl --user enable --now alchemyui-auth.service
    say "auth edge up -- friends reach AlchemyUI at  ${redirect%/oauth2/callback}"
  else
    warn "once Dex has the client, start the edge:  ALCHEMYUI_START_AUTH=1 ./deploy/install.sh"
  fi
  if grep -qE '^DEV_MODE=(1|true|yes|on)' "$REPO/.env"; then
    warn "DEV_MODE is still set in .env: behind oauth2-proxy, unset it, or every"
    warn "visitor is treated as the one dev identity."
  fi
fi
```

  - Renumber the old "7. sanity" to "8. sanity". Replace the final echo block's "Single user…" lines with:

```bash
echo "  Locally: DEV_MODE=1 gives one fake identity, no login."
echo "  For friends over NetBird: the auth edge above, then unset DEV_MODE."
```

- [ ] **Step 8: `deploy/alchemyui-cli.sh`.**
  - Replace the header's "Deliberately one unit…" paragraph with: "Two units: the app, and its oauth2-proxy auth edge (only if installed). ComfyUI keeps its own lifetime."
  - Replace `UNIT=alchemyui.service` with:

```bash
UNITS=(alchemyui.service)
[ -f "$HOME/.config/systemd/user/alchemyui-auth.service" ] && \
  systemctl --user is-enabled -q alchemyui-auth.service 2>/dev/null && UNITS+=(alchemyui-auth.service)
```

  - In `cmd_status`, loop `for UNIT in "${UNITS[@]}"; do … done` around the state print.
  - In the `case`: use `"${UNITS[@]}"` for `start`, `stop` and `restart`; for `logs` use `exec journalctl --user $(printf -- '-u %s ' "${UNITS[@]}") -f`.
  - In `usage()`, change `Unit: $UNIT` to `Units: ${UNITS[*]}`.

- [ ] **Step 9: `.gitignore`** — add under the secrets section:

```
deploy/oauth2-proxy.env
```

- [ ] **Step 10: `.env.example`.** Replace the Identity block with:

```
# ---------------------------------------------------------------------------
# Identity (v2: multi-user)
# ---------------------------------------------------------------------------
# Locally: DEV_MODE=1 and one fake identity, no login. For friends over
# NetBird, oauth2-proxy (deploy/install.sh) authenticates against
# Llamacracy's Dex and forwards the user; unset DEV_MODE then -- the app
# requires the X-Forwarded-* headers and refuses to serve without them, so it
# can never accidentally run open. Users are keyed on the OIDC sub.
DEV_MODE=1
DEV_EMAIL=dev@localhost
DEV_NAME=Dev User

# Comma-separated. Who may open /admin. Include DEV_EMAIL for local use. The
# first admin to sign in inherits every image and job v1 left behind;
# `alchemyui reassign FROM TO` moves them later if that was the wrong account.
ADMIN_EMAILS=dev@localhost

# ---------------------------------------------------------------------------
# Metering -- visibility, never limits
# ---------------------------------------------------------------------------
# Each job's energy = (measured GPU draw + NON_GPU_LOAD_WATTS) x the time it
# held the GPU. CPU is estimated because Intel RAPL is root-only here.
NON_GPU_LOAD_WATTS=25
# Used only when nvidia-smi gave no reading for a job.
GPU_FALLBACK_WATTS=60
# $/kWh. TOU_SCHEDULE, if set, wins: JSON of local hour -> $/kWh, e.g.
# {"0":0.31,"16":0.52,"21":0.31}; hours it omits use ELECTRICITY_RATE.
ELECTRICITY_RATE=0.30
TOU_SCHEDULE=
```

- [ ] **Step 11: Run the tests and check the shell.** `uv run pytest -q` → all pass; `bash -n deploy/install.sh deploy/alchemyui-cli.sh` → no syntax errors; `shellcheck deploy/*.sh` if available.

- [ ] **Step 12: Commit.** `git commit -m "Phase 6: auth edge (oauth2-proxy -> Llamacracy's Dex), installer, ctl, reassign"`

---

### Task 11: Live acceptance against the real ComfyUI, and docs

**Files:**
- Modify: `README.md`, `docs/DECISIONS.md`, and the user's local `.env` (gitignored: add `ADMIN_EMAILS`)

This task runs against real hardware; no mocks. Start ComfyUI in the background (`cd ~/ComfyUI && ./venv/bin/python main.py --fp8_e4m3fn-unet --preview-method auto`). Restart AlchemyUI's unit so it picks up the new code.

- [ ] **Step 1: Registry against the real server.** `uv run alchemyui validate --live` → every checkpoint `present`, including the folder-pathed ones, and no rejected sampler choices (`dpmpp_sde` accepted).

- [ ] **Step 2: Local `.env`.** Append `ADMIN_EMAILS=<the current DEV_EMAIL>` to the gitignored `.env` so the running dev identity is an admin, and `systemctl --user restart alchemyui`. Load `/` → the topbar shows the name and an Admin link, and the v1 gallery is still there (claimed on that first admin request). Check with `sqlite3 data/alchemyui.db "SELECT COUNT(*) FROM images WHERE user_id IS NULL"` → 0.

- [ ] **Step 3: Two users, real app, forwarded headers.** Run a second app instance with `DEV_MODE=` on another port (e.g. `DEV_MODE= PORT=8301 DATABASE_PATH=<scratch db> OUTPUT_DIR=<scratch> uv run uvicorn alchemyui.app:app --port 8301 --timeout-graceful-shutdown 5`). With `curl -H 'X-Forwarded-User: …' -H 'X-Forwarded-Email: …'`:
  - A submits an SD1.5 job and B submits one;
  - B's `/api/images` never contains A's image, and B's `/api/events` never mentions A's job id;
  - with A holding a job on SDXL, B queued on SD1.5 and A's second job on SD1.5, the run order is A1, B1, A2 (no cross-user promotion).

- [ ] **Step 4: Metering is plausible.** After those runs, `SELECT gpu_watts_mean, occupancy_seconds, energy_wh, cost_usd FROM jobs ORDER BY finished_at DESC LIMIT 3` → watts between about 20 and 90 on this card; energy consistent with the formula.

- [ ] **Step 5: Admin page live.** Open `/admin` (the headless-Firefox request-log method from the v1 blank-page fix, or `curl /api/admin/live` in a loop) during a real generation → GPU watts climb above idle, and the running job shows its owner.

- [ ] **Step 6: Inpainting without a seam.** Through the v1 UI's Paint-over mode (or `/api/uploads` + `/api/uploads/{id}/mask` + `/api/generate`), inpaint a region on the RV6 inpainting checkpoint. Open the result and look at the mask boundary. Record the timing.

- [ ] **Step 7: Hyper sampling, measured.** Generate on `Realistic Vision Hyper` (6 steps, cfg 1.5, dpmpp_sde) and on RV 6.0 (25 steps). Record the wall time and look at both images. If the Hyper output is burnt or mushy, adjust `steps`/`cfg` in `registry.json` from the measurement, and write down what was tried.

- [ ] **Step 8: Docs.**
  - `README.md`:
    - The "Mid-redesign" note now says Phase 6 is in.
    - Add a short **Accounts and the admin page** section: DEV_MODE vs the auth edge, `ADMIN_EMAILS`, `/admin`, and `alchemyui reassign`.
    - Update the Models section's checkpoint list to the folder paths and the inpaint default, and remove the "validate will report missing" caveat.
    - Update the Development test count.
    - Change the Layout block to list `identity.py`, `admin.py`, `power.py` and `meter.py`.
  - `docs/DECISIONS.md`: add a **Phase 6 — as built** section with anything the live run taught: measured Hyper timings and settings, measured watts, and the polling-vs-SSE choice for the admin page (polling, as Llamacracy does, which also gates `nvidia-smi` on someone looking).
  - `ALCHEMYUI_SPEC.md` §11 says the admin page is "live over its own SSE stream". Change it to "live, polled every 2s (Llamacracy's pattern)" to match what was built.

- [ ] **Step 9: Final checks and commit.** `uv run pytest -q`, `uv run ruff check .`, `node --check` on both JS files → all clean. Stop the scratch instance and ComfyUI. Commit with `git commit -m "Phase 6: live acceptance notes, README and DECISIONS"`.

---

## Self-review

- **Spec coverage (§14, Phase 6):**
  - users and identity → Task 2;
  - `user_id` everywhere, scoped endpoints and SSE, anonymised snapshot → Tasks 1, 4, 5;
  - v1 claim → Task 2;
  - fairness → Task 3;
  - catalogue, traits, `sd15-inpaint`, Hyper → Task 6;
  - metering with TOU and frozen cost → Task 7;
  - admin page and API, disable → Tasks 8 and 9;
  - user usage → Task 8;
  - deploy artifacts → Task 10;
  - acceptance → Task 11;
  - executed front-end boot (§12, §16.7) → Task 9.
  - Deliberately **not** here: `refs.is_likeness` and `turn_id` belong to Phase 7, where they are first used.
- **Types:**
  - `EVERYONE`, `snapshot(viewer)` and `subscribe(viewer)` are used consistently in Tasks 4, 5 and 8.
  - `cancel(job_id, user_id=None)`, `cancel_user` and `submit(..., user_id=None)` match across Tasks 3, 5 and 8.
  - `finalise(picked_at, finished_at, gpu_watts_mean, settings) -> Reading` is the same in Tasks 7 and 8.
  - `FixedSampler` is defined in conftest and used by `_wire` and `_make_qm`.
- **Known judgement calls, taken rather than asked:**
  - the admin page polls instead of using SSE (to match Llamacracy; spec wording updated in Task 11);
  - `NON_GPU_LOAD_WATTS` defaults to 25 W for a laptop, not Llamacracy's 110 W desktop figure;
  - `ELECTRICITY_RATE` keeps Llamacracy's 0.30 default, and the owner should set their real rate.
