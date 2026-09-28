"""The model switcher: the menu's list, a pin carried by a turn and its thread,
and covers served only for what the Lab adopted."""

from __future__ import annotations

import json

import pytest

from alchemyui.planner.router import RouteResult
from alchemyui.planner.schema import Plan
from tests.conftest import A, B, wait_for

pytestmark = pytest.mark.env(DEV_MODE="", ADMIN_EMAILS="alice@example.com")

RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
EPIC = "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"


def P(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="1:1", count=1, strength="medium", quality="fast", direction="",
                question="", options=())
    base.update(kw)
    return Plan(**base)


def _done(client, who, thread_id):
    def check():
        turns = client.get(f"/api/threads/{thread_id}", headers=who).json()["turns"]
        return turns if turns[-1]["state"] == "done" else None
    return wait_for(client, check)


def test_the_menu_lists_the_photoreal_models_with_times(client):
    r = client.get("/api/models", headers=A)
    assert r.status_code == 200
    models = r.json()["models"]
    assert [m["name"] for m in models][0] == XL and len(models) == 4
    for m in models:
        assert m["label"] and m["tagline"] and m["eta_s"] > 0
        assert m["resident"] is False and m["cover_url"] is None
    # SDXL is slower than SD1.5, and the times say so
    by = {m["name"]: m for m in models}
    assert by[XL]["eta_s"] > by[RV6]["eta_s"]


def test_a_pinned_turn_runs_on_the_pinned_model_and_the_thread_remembers(client):
    client.router.results = [RouteResult(P(), "primary"), RouteResult(P(), "primary")]
    t = client.post("/api/turns", headers=A, json={"text": "a lighthouse", "model": EPIC}).json()
    assert t["pin"] == EPIC
    turns = _done(client, A, t["thread_id"])
    assert turns[0]["compiled"]["checkpoint"] == EPIC
    assert turns[0]["chip"] == "Generating · epiCRealism · 1:1"
    assert turns[0]["images"][0]["params"]["checkpoint"] == EPIC

    thread = client.get("/api/threads", headers=A).json()["threads"][0]
    assert thread["model"] == EPIC
    assert thread["cover_url"] == turns[0]["images"][0]["url"]

    # Auto again: the thread follows the latest pick
    t2 = client.post("/api/turns", headers=A,
                     json={"text": "again", "thread_id": t["thread_id"], "model": None}).json()
    assert t2["pin"] is None
    assert client.get(f"/api/threads/{t['thread_id']}", headers=A).json()["thread"]["model"] is None


def test_an_unknown_or_unswitchable_model_is_refused(client):
    for bad in ("nope.safetensors", "SD1.5/v1-5-pruned-emaonly.safetensors"):
        r = client.post("/api/turns", headers=A, json={"text": "x", "model": bad})
        assert r.status_code == 400, bad


def test_a_note_explains_a_pick_that_was_not_used(client):
    client.router.results = [RouteResult(P(action="likeness", faces="me"), "primary")]
    client.get("/api/me", headers=A)          # alice exists from here on
    uid = client.db._conn.execute("SELECT id FROM users WHERE oidc_sub='sub-alice'").fetchone()[0]
    client.db._conn.execute(
        "INSERT INTO refs (id, user_id, path, comfy_name, width, height, is_likeness, created_at) "
        "VALUES ('r1', ?, 'x', 'alchemyui/r1.png', 768, 768, 1, 1)", (uid,))
    t = client.post("/api/turns", headers=A, json={"text": "me as an astronaut", "model": RV6}).json()
    turns = _done(client, A, t["thread_id"])
    assert turns[0]["compiled"]["checkpoint"] == XL
    assert "SDXL" in turns[0]["note"]


def test_a_cover_is_served_to_anyone_but_only_for_an_adopted_lab_image(client):
    client.router.results = [RouteResult(P(), "primary")]
    t = client.post("/api/turns", headers=A, json={"text": "a lighthouse"}).json()
    img = _done(client, A, t["thread_id"])[0]["images"][0]
    conn = client.db._conn
    # an ordinary image is never a cover, even if a profile names it
    conn.execute("INSERT INTO model_profiles (checkpoint, settings_json, cover_image_id, "
                 "updated_at) VALUES (?, ?, ?, 1)", (RV6, json.dumps({}), img["id"]))
    assert client.get(f"/api/models/cover?name={RV6}", headers=B).status_code == 404
    # a lab image that a profile adopted is, for every signed-in person
    conn.execute("UPDATE images SET lab_run_id = 'run1' WHERE id = ?", (img["id"],))
    r = client.get(f"/api/models/cover?name={RV6}", headers=B)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    by = {m["name"]: m for m in client.get("/api/models", headers=B).json()["models"]}
    assert by[RV6]["cover_url"].startswith("/api/models/cover?name=")
