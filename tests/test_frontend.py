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


def _boot(base: str, script: str) -> dict:
    out = subprocess.run([NODE, str(REPO / "tests" / "js" / "boot.mjs"), base, str(ASSETS / script)],
                         capture_output=True, text=True, timeout=60)
    lines = out.stdout.strip().splitlines()
    assert lines, f"no output from node: {out.stderr}"
    return json.loads(lines[-1])


@pytest.mark.parametrize("script, first_call", [("app.js", "/api/me"),
                                                ("admin.js", "/api/admin/live")])
def test_page_boots_and_renders(live_server, script, first_call):
    base, _, _ = live_server
    result = _boot(base, script)
    assert result["error"] is None, result["error"]
    assert result["rendered"] > 0
    assert first_call in result["requests"]
