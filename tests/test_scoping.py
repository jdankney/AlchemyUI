"""Everything a user owns is served only to them: gallery, jobs, uploads,
references, presets, history -- and the event stream."""

from __future__ import annotations

import io
import json
import time
import uuid

import httpx
import pytest
from PIL import Image

from tests.conftest import A, B, wait_for

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
