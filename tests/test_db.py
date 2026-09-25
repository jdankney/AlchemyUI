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
