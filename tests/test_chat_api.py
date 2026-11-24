"""The chat API over HTTP, as two users behind oauth2-proxy's headers:
threads and turns are private, attachments must be your own, likenesses
are capped at four, and the admin sees router health without anyone's words."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

import alchemyui.app as appmod
from alchemyui.planner.router import RouteResult
from alchemyui.planner.schema import Plan
from tests.conftest import A, B, wait_for

pytestmark = pytest.mark.env(DEV_MODE="", ADMIN_EMAILS="alice@example.com")


def P(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse, night",
                negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                direction="", question="", options=())
    base.update(kw)
    return Plan(**base)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 200, 90)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, who) -> str:
    r = client.post("/api/uploads", headers=who, files={"file": ("x.png", _png(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _turn(client, who, text, **extra):
    r = client.post("/api/turns", headers=who, json={"text": text, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _wait_state(client, who, thread_id, state="done"):
    def check():
        turns = client.get(f"/api/threads/{thread_id}", headers=who).json()["turns"]
        return turns if turns[-1]["state"] == state else None
    return wait_for(client, check)


def test_turn_round_trip_over_http(client):
    client.router.results = [RouteResult(P(), "primary", 700, 90, 30)]
    t = _turn(client, A, "Generate me a photo of a spooky lighthouse at night")
    assert t["state"] == "planning" and t["seq"] == 1
    turns = _wait_state(client, A, t["thread_id"])
    assert turns[0]["images"][0]["handle"] == "t1.1"
    assert turns[0]["chip"].startswith("Generating")
    threads = client.get("/api/threads", headers=A).json()["threads"]
    assert threads[0]["id"] == t["thread_id"]
    assert threads[0]["title"].startswith("Generate me a photo")
    # the image is in the library too, with its turn
    img = client.get("/api/images", headers=A).json()["images"][0]
    assert img["turn_id"] == t["id"]


def test_threads_and_turns_are_private(client):
    client.router.results = [RouteResult(P(), "primary")]
    t = _turn(client, A, "alice's lighthouse")
    _wait_state(client, A, t["thread_id"])
    assert client.get("/api/threads", headers=B).json()["threads"] == []
    assert client.get(f"/api/threads/{t['thread_id']}", headers=B).status_code == 404
    assert client.delete(f"/api/threads/{t['thread_id']}", headers=B).status_code == 404
    for action in ("cancel", "retry"):
        assert client.post(f"/api/turns/{t['id']}/{action}", headers=B).status_code == 404
    assert client.post(f"/api/turns/{t['id']}/answer", headers=B,
                       json={"text": "x"}).status_code == 404
    r = client.post("/api/turns", headers=B, json={"text": "x", "thread_id": t["thread_id"]})
    assert r.status_code == 404


def test_attachments_limit_and_ownership(client):
    mine = [_upload(client, A) for _ in range(5)]
    r = client.post("/api/turns", headers=A, json={"text": "x", "attachments": mine})
    assert r.status_code == 400 and "at most 4" in r.json()["detail"]
    r = client.post("/api/turns", headers=B, json={"text": "x", "attachments": mine[:1]})
    assert r.status_code == 404
    r = client.post("/api/turns", headers=A, json={"text": "  "})
    assert r.status_code == 400


def test_answer_cancel_retry_and_delete(client):
    client.router.results = [RouteResult(P(action="ask", question="Which?", options=("A", "B")),
                                         "primary"),
                             RouteResult(P(), "primary")]
    t = _turn(client, A, "hmm")
    turns = _wait_state(client, A, t["thread_id"], "needs_input")
    assert turns[0]["question"] == {"question": "Which?", "options": ["A", "B"],
                                      "candidates": []}
    r = client.post(f"/api/turns/{t['id']}/answer", headers=A, json={"option": 0})
    assert r.status_code == 200
    _wait_state(client, A, t["thread_id"])
    assert client.post(f"/api/turns/{t['id']}/cancel", headers=A).status_code == 409
    assert client.post(f"/api/turns/{t['id']}/retry", headers=A).status_code == 200
    assert client.delete(f"/api/threads/{t['thread_id']}", headers=A).status_code == 200
    assert client.get("/api/threads", headers=A).json()["threads"] == []


def test_likeness_is_capped_at_four_and_lists_only_likenesses(client, monkeypatch):
    monkeypatch.setattr(appmod, "get_analyser", lambda settings=None: None)
    monkeypatch.setattr(appmod, "normalise_reference", lambda data, s, a, label: (_png(), {
        "width": 768, "height": 768, "faces_found": 1, "face_score": 0.9, "face_px": 200}))
    for i in range(4):
        r = client.post("/api/references", headers=A,
                        files={"file": (f"me{i}.png", _png(), "image/png")})
        assert r.status_code == 200, r.text
    r = client.post("/api/references", headers=A, files={"file": ("me5.png", _png(), "image/png")})
    assert r.status_code == 400 and "up to 4" in r.json()["detail"]

    uid = client.db._conn.execute("SELECT id FROM users WHERE oidc_sub='sub-alice'").fetchone()[0]
    client.db._conn.execute(
        "INSERT INTO refs (id, user_id, path, comfy_name, width, height, is_likeness, created_at) "
        "VALUES ('adhoc', ?, 'p', 'c', 768, 768, 0, 1)", (uid,))
    ids = [x["id"] for x in client.get("/api/references", headers=A).json()["references"]]
    assert len(ids) == 4 and "adhoc" not in ids
    assert client.get("/api/references", headers=B).json()["references"] == []


def test_admin_sees_router_status_and_fallbacks_without_anyones_words(client):
    client.get("/api/me", headers=A)
    client.router.results = [RouteResult(P(prompt="bob's secret prompt"), "fallback", 3000,
                                         errors=["primary: primary unreachable"])]
    t = _turn(client, B, "bob's secret words")
    _wait_state(client, B, t["thread_id"])
    live = client.get("/api/admin/live", headers=A).json()
    assert "router" in live and "planning" in live["router"]
    body = client.get("/api/admin/fallbacks", headers=A).json()
    row = body["fallbacks"][0]
    assert row["owner"] == "bob" and row["plan_source"] == "fallback"
    assert "unreachable" in row["plan_error"]
    blob = json.dumps(body)
    assert "secret" not in blob
    assert client.get("/api/admin/fallbacks", headers=B).status_code == 403
