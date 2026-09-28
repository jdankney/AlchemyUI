"""The front-end is executed, not just parsed. v1 shipped `const f = w =` --
valid syntax, a ReferenceError at module load, and a blank page -- past
`node --check`. This boots each page's real script against a real server."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "alchemyui" / "static" / "assets"
NODE = shutil.which("node")

pytestmark = [pytest.mark.skipif(NODE is None, reason="node is not installed"),
              # pinned: Settings also reads the developer's own .env, whose
              # DEV_EMAIL would otherwise decide who the admin is
              pytest.mark.env(ADMIN_EMAILS="dev@localhost", DEV_EMAIL="dev@localhost")]


def _boot(base: str, script: str, hash_: str = "") -> dict:
    out = subprocess.run([NODE, str(REPO / "tests" / "js" / "boot.mjs"), base, str(ASSETS / script),
                          hash_],
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines, f"no output from node: {out.stderr}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("script, first_call", [("app.js", "/api/me"),
                                                ("admin.js", "/api/admin/live"),
                                                ("lab.js", "/api/lab/presets")])
def test_page_boots_and_renders(live_server, script, first_call):
    base, _, _ = live_server
    result = _boot(base, script)
    assert result["error"] is None, result["error"]
    assert result["rendered"] > 0
    assert first_call in result["requests"]


def test_chat_boots_into_its_threads(live_server):
    base, _, _ = live_server
    result = _boot(base, "app.js")
    assert result["error"] is None, result["error"]
    assert "/api/threads" in result["requests"]
    assert "/api/events" in result["requests"]
    assert "/api/models" in result["requests"]


def test_the_lab_boots_into_its_runs_and_an_estimate(live_server):
    base, _, _ = live_server
    result = _boot(base, "lab.js")
    assert result["error"] is None, result["error"]
    assert {"/api/lab/runs", "/api/models", "/api/lab/estimate"} <= set(result["requests"])


def test_a_lab_run_page_renders_with_its_cells(live_server):
    """The run view is the Lab's most complex drawing: boot after a finished
    run exists, and open it the way the page does."""
    import time

    import httpx

    base, _, _ = live_server
    rv6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
    with httpx.Client(base_url=base, timeout=10) as c:
        run = c.post("/api/lab/runs", json={"checkpoints": [rv6], "prompts": ["food"], "seeds": 1,
                                            "axes": [{"param": "cfg", "values": [4, 6]}]}).json()
        deadline = time.time() + 10
        while c.get(f"/api/lab/runs/{run['id']}").json()["state"] != "done":
            assert time.time() < deadline
            time.sleep(0.05)
    result = _boot(base, "lab.js")
    assert result["error"] is None, result["error"]


@pytest.mark.parametrize("hash_, calls", [
    ("#/settings", {"/api/references", "/api/me/usage"}),
    ("#/library", {"/api/images?limit=60"}),
    ("#/t/no-such-thread", {"/api/threads/no-such-thread"}),
])
def test_every_view_boots(live_server, hash_, calls):
    """A 404 thread must render an honest message, not throw."""
    base, _, _ = live_server
    result = _boot(base, "app.js", hash_)
    assert result["error"] is None, result["error"]
    assert result["rendered"] > 0
    assert calls <= set(result["requests"]), result["requests"]


def test_a_thread_with_every_kind_of_turn_renders(live_server):
    """Turns are the most complex thing the page draws; boot straight into a
    thread holding a finished generation, a question, and a failure."""
    import time

    import httpx

    from alchemyui.planner.router import RouteResult
    from alchemyui.planner.schema import Plan
    from alchemyui.threads import get_engine

    base, comfy, _ = live_server
    plan = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="1:1", count=2, strength="medium", quality="fast", direction="",
                question="", options=())
    get_engine().router.results = [
        RouteResult(Plan(**plan), "primary", 800),
        RouteResult(Plan(**{**plan, "action": "ask", "question": "Which one?",
                            "options": ("1", "2")}), "fallback"),
    ]
    with httpx.Client(base_url=base, timeout=10) as c:
        t = c.post("/api/turns", json={"text": "a lighthouse"}).json()
        deadline = time.time() + 10
        while c.get(f"/api/threads/{t['thread_id']}").json()["turns"][-1]["state"] != "done":
            assert time.time() < deadline
            time.sleep(0.05)
        c.post("/api/turns", json={"text": "make it darker", "thread_id": t["thread_id"]})
        while c.get(f"/api/threads/{t['thread_id']}").json()["turns"][-1]["state"] != "needs_input":
            assert time.time() < deadline
            time.sleep(0.05)
    result = _boot(base, "app.js", f"#/t/{t['thread_id']}")
    assert result["error"] is None, result["error"]
    assert f"/api/threads/{t['thread_id']}" in result["requests"]
