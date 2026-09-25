"""HTTP-level tests against the real FastAPI app with a scripted ComfyUI:
the submit -> SSE -> gallery contract, cancellation, error reporting, and the
recovery endpoints the browser leans on after a reconnect.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from tests.conftest import wait_for as _wait


def _sse_events(response, stop_on, limit=80):
    out = []
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        event = json.loads(line[5:].strip())
        out.append(event)
        if stop_on(event) or len(out) >= limit:
            break
    return out


# --- workflows ---------------------------------------------------------------
def test_workflows_expose_controls_but_never_the_graph(client):
    body = client.get("/api/workflows").json()
    w = body["workflows"][0]
    assert w["id"] == "txt2img-sdxl"
    assert set(w["aspects"]) == {"1:1", "3:4", "4:3", "9:16", "16:9"}
    assert w["aspects"]["16:9"] == [1344, 768]
    assert w["defaults"]["steps"] == 25
    assert w["est_seconds"] > 0                      # an honest headline number
    # node ids, template names and the graph itself stay server-side
    assert not {"params", "template", "output_node", "progress_node"} & set(w)


def test_healthz_reports_comfyui(client):
    assert client.get("/healthz").json() == {"ok": True, "comfyui": True}


# --- the happy path ----------------------------------------------------------
def test_generate_then_image_lands_in_the_gallery(client):
    r = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "a lighthouse", "aspect": "16:9", "seed": 99,
    })
    assert r.status_code == 200
    body = r.json()
    job_id = body["job_id"]
    assert body["position"] is not None
    # the resolved parameter set comes straight back, so the UI can show it
    assert body["params"]["width"] == 1344 and body["params"]["seed"] == 99

    images = _wait(client, lambda: client.get("/api/images").json()["images"])
    img = images[0]
    assert img["job_id"] == job_id
    assert img["seed"] == 99
    assert img["params"]["prompt"] == "a lighthouse"

    # and it survives a reload: served from app-owned storage, not ComfyUI
    f = client.get(img["url"])
    assert f.status_code == 200
    assert f.content.startswith(b"\x89PNG")

    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["id"] == job_id and jobs[0]["status"] == "completed"


def test_submitted_graph_carries_the_injected_parameters(client):
    client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "a red bicycle", "aspect": "3:4", "seed": 7,
    })
    _wait(client, lambda: client.comfy.submitted)
    prompt_id, graph = client.comfy.submitted[0]
    assert graph["6"]["inputs"]["text"] == "a red bicycle"
    assert graph["3"]["inputs"]["seed"] == 7
    assert (graph["5"]["inputs"]["width"], graph["5"]["inputs"]["height"]) == (896, 1152)
    assert graph["4"]["inputs"]["ckpt_name"] == "SDXL/RealVisXL_V5.0_fp16.safetensors"
    # we mint the prompt_id, and it matches the job the caller was handed
    assert prompt_id == client.get("/api/jobs").json()["jobs"][0]["id"]


def test_sse_stream_carries_queue_progress_and_the_image(live_server):
    base, comfy, _ = live_server
    comfy.steps, comfy.delay = 40, 0.05

    with httpx.Client(base_url=base, timeout=30) as c:
        c.post("/api/generate",
               json={"workflow_id": "txt2img-sdxl", "prompt": "a fox", "steps": 40})
        events = []
        deadline = time.time() + 25
        with c.stream("GET", "/api/events") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
                    if events[-1]["type"] == "image":
                        break
                if time.time() > deadline:
                    raise AssertionError(f"no image event; saw {[e['type'] for e in events]}")

    types = [e["type"] for e in events]
    assert types[0] == "queue"            # a snapshot the moment we connect
    assert "progress" in types
    assert types[-1] == "image"

    progress = [e for e in events if e["type"] == "progress"]
    assert progress[-1]["value"] == progress[-1]["max"] == 40   # counted all the way up
    assert progress[-1]["eta_s"] is not None                    # an ETA, not a spinner

    # the opening snapshot describes the job already in flight, so a browser
    # that reconnects mid-generation is never shown an empty queue
    assert events[0]["jobs"] and events[0]["jobs"][0]["position"] == 0


def test_sse_survives_a_reconnect_and_state_is_recoverable(live_server):
    """A browser that drops mid-generation reconnects and rebuilds from
    /api/jobs + /api/images, which is exactly what app.js does."""
    base, comfy, _ = live_server
    comfy.steps, comfy.delay = 40, 0.05

    with httpx.Client(base_url=base, timeout=30) as c:
        job_id = c.post("/api/generate",
                        json={"workflow_id": "txt2img-sdxl", "prompt": "gone", "steps": 40}
                        ).json()["job_id"]
        with c.stream("GET", "/api/events") as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    break                       # connect, read one frame, drop

        jobs = c.get("/api/jobs").json()["jobs"]
        assert jobs[0]["id"] == job_id
        assert jobs[0]["status"] in ("queued", "running")

        # reconnecting gets a fresh snapshot, not silence
        with c.stream("GET", "/api/events") as stream:
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    snap = json.loads(line[5:].strip())
                    break
        assert snap["type"] == "queue"
        assert [j["id"] for j in snap["jobs"]] == [job_id]


# --- queue behaviour ---------------------------------------------------------
def test_second_job_reports_a_real_queue_position(client):
    client.comfy.delay = 0.1
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "first"})
    second = client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "second"})
    assert second.json()["position"] == 1

    snap = client.get("/api/queue").json()
    assert [j["position"] for j in snap["jobs"]] == [0, 1]
    assert snap["jobs"][1]["eta_s"] > 0


def test_cancel_a_running_job(client):
    client.comfy.steps = 200
    client.comfy.delay = 0.02
    job_id = client.post(
        "/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "slow", "steps": 60}
    ).json()["job_id"]

    _wait(client, lambda: client.comfy.submitted)
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    assert client.comfy.interrupts == [job_id]

    status = _wait(client, lambda: next(
        (j["status"] for j in client.get("/api/jobs").json()["jobs"]
         if j["id"] == job_id and j["status"] != "running"), None))
    assert status == "cancelled"
    assert client.get("/api/images").json()["images"] == []


def test_cancelling_an_unknown_job_is_404(client):
    assert client.post("/api/jobs/nope/cancel").status_code == 404


# --- failure surfaces --------------------------------------------------------
def test_execution_error_gives_a_readable_message_and_keeps_the_traceback(client):
    client.comfy.mode = "error"
    job_id = client.post(
        "/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "boom"}
    ).json()["job_id"]

    job = _wait(client, lambda: next(
        (j for j in client.get("/api/jobs").json()["jobs"]
         if j["id"] == job_id and j["status"] == "failed"), None))
    assert "KSampler" in job["error_text"]
    assert "same device" in job["error_text"]
    assert "traceback" in job["error_detail"]      # still there for the disclosure


def test_rejected_graph_names_the_offending_field(client):
    client.comfy.mode = "reject"
    job_id = client.post(
        "/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "bad"}
    ).json()["job_id"]

    job = _wait(client, lambda: next(
        (j for j in client.get("/api/jobs").json()["jobs"]
         if j["id"] == job_id and j["status"] == "failed"), None))
    assert "sampler_name" in job["error_text"]


def test_unknown_workflow_is_rejected_before_anything_is_written(client):
    r = client.post("/api/generate", json={"workflow_id": "nope", "prompt": "x"})
    assert r.status_code == 400
    assert client.db._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_empty_prompt_is_rejected(client):
    assert client.post(
        "/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "   "}
    ).status_code == 400
    assert client.post(
        "/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": ""}
    ).status_code == 422       # pydantic min_length


# --- gallery -----------------------------------------------------------------
def test_delete_removes_the_row_and_the_file(client):
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "bye"})
    img = _wait(client, lambda: client.get("/api/images").json()["images"])[0]
    path = Path(client.db._conn.execute(
        "SELECT path FROM images WHERE id=?", (img["id"],)).fetchone()[0])
    assert path.exists()

    assert client.post if False else client.delete(f"/api/images/{img['id']}").status_code == 200
    assert client.get("/api/images").json()["images"] == []
    assert not path.exists()
    assert client.get(img["url"]).status_code == 404


def test_missing_file_on_disk_is_reported_distinctly(client):
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "vanish"})
    img = _wait(client, lambda: client.get("/api/images").json()["images"])[0]
    Path(client.db._conn.execute(
        "SELECT path FROM images WHERE id=?", (img["id"],)).fetchone()[0]).unlink()
    # 410, not 404: the row exists, the bytes are gone -- a different problem
    assert client.get(img["url"]).status_code == 410


def test_seed_stays_within_what_sqlite_can_store(client):
    """A random seed used to overflow SQLite's signed 64-bit INTEGER on the
    insert, killing roughly half of all unseeded generations."""
    for _ in range(6):
        client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "seeds"})
    images = _wait(client, lambda: (
        client.get("/api/images").json()["images"]
        if len(client.get("/api/images").json()["images"]) == 6 else None))
    for img in images:
        assert 0 <= img["seed"] <= 0x7FFFFFFFFFFFFFFF
