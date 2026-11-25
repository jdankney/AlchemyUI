"""SQLite access. One connection, WAL mode, serialised through a single
asyncio lock -- writes are tiny (a job row, an image row) and the queue is
strictly serial anyway, so this is plenty and keeps the code simple.

All timestamps are epoch floats, UTC, throughout the app.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import get_settings

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

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
    # v2 / Phase 7: turns own jobs and images; reference faces become the
    # saved likeness. DEFAULT 1 is deliberate: every v1 reference was one.
    "ALTER TABLE jobs ADD COLUMN turn_id TEXT",
    "ALTER TABLE images ADD COLUMN turn_id TEXT",
    "ALTER TABLE refs ADD COLUMN is_likeness INTEGER NOT NULL DEFAULT 1",
    # Phase 7.5: the model a person pinned; the colours an image glows in; the
    # Lab's low-priority lane and the runs its images belong to.
    "ALTER TABLE threads ADD COLUMN model TEXT",
    "ALTER TABLE turns ADD COLUMN model TEXT",
    "ALTER TABLE images ADD COLUMN palette TEXT",
    "ALTER TABLE images ADD COLUMN lab_run_id TEXT",
    "ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'user'",
    "ALTER TABLE jobs ADD COLUMN lab_run_id TEXT",
    # Release prep: a retried turn settles on its new jobs only.
    "ALTER TABLE turns ADD COLUMN attempt_at REAL",
]

# Indexes on migrated columns run AFTER the ALTERs: schema.sql is applied
# first, and an index on a column an old DB does not have yet would abort
# startup before the migration that adds it ever ran.
_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_jobs_user_finished ON jobs(user_id, finished_at)",
    "CREATE INDEX IF NOT EXISTS ix_images_user_created ON images(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_uploads_user ON uploads(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_refs_user ON refs(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_turn ON jobs(turn_id)",
    "CREATE INDEX IF NOT EXISTS ix_images_turn ON images(turn_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_images_lab ON images(lab_run_id)",
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


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        _rebuild_presets(self._conn)
        for stmt in _INDEXES:
            self._conn.execute(stmt)
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # --- low level (call under _lock via the async helpers) ----------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        async with self._lock:
            self._exec(sql, params)

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        async with self._lock:
            return self._exec(sql, params).fetchone()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return self._exec(sql, params).fetchall()

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._lock:
            cur = self._exec(sql, params)
            return int(cur.lastrowid or 0)

    async def transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        async with self._lock:
            try:
                self._conn.execute("BEGIN")
                for sql, params in statements:
                    self._exec(sql, params)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_path)
    return _db


def now() -> float:
    """Canonical time source: Unix seconds, UTC."""
    return time.time()
