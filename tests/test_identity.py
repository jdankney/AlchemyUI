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
