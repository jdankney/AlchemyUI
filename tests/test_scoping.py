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
