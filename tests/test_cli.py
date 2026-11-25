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
    c.execute("INSERT INTO threads (id, user_id, created_at, updated_at) VALUES ('th', 1, 1, 1)")
    c.execute("INSERT INTO turns (id, thread_id, user_id, seq, state, created_at) "
              "VALUES ('tu', 'th', 1, 1, 'done', 1)")
    db.close()

    assert main(["reassign", "old@x", "new@x"]) == 0
    db = Database(tmp_db_path)
    assert db._conn.execute("SELECT user_id FROM jobs").fetchone()[0] == 2
    assert db._conn.execute("SELECT user_id FROM presets").fetchone()[0] == 2
    # chat history follows the person, or the new account's rail is empty
    assert db._conn.execute("SELECT user_id FROM threads").fetchone()[0] == 2
    assert db._conn.execute("SELECT user_id FROM turns").fetchone()[0] == 2
    get_settings.cache_clear()


def test_reassign_refuses_unknown_accounts(tmp_db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", tmp_db_path)
    from alchemyui.config import get_settings
    get_settings.cache_clear()
    Database(tmp_db_path).close()
    assert main(["reassign", "nobody@x", "also-nobody@x"]) == 1
    get_settings.cache_clear()
