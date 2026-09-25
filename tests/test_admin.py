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
