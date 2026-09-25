from __future__ import annotations

import asyncio
import base64
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent

import alchemyui.app as appmod  # noqa: E402
import alchemyui.queue as queuemod  # noqa: E402
from alchemyui.comfy import get_comfy  # noqa: E402
from alchemyui.config import get_settings  # noqa: E402
from alchemyui.db import Database, get_db  # noqa: E402
from alchemyui.registry import get_registry, load_registry  # noqa: E402

os.environ.setdefault("DEV_MODE", "1")

# A real 1x1 PNG, so storage.png_size() has something honest to parse.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# Two identities as oauth2-proxy would forward them. Tests that use these run
# with DEV_MODE unset (the `env` marker), so the app trusts only the headers.
A = {"X-Forwarded-User": "sub-alice", "X-Forwarded-Email": "alice@example.com",
     "X-Forwarded-Preferred-Username": "alice"}
B = {"X-Forwarded-User": "sub-bob", "X-Forwarded-Email": "bob@example.com",
     "X-Forwarded-Preferred-Username": "bob"}


async def add_user(db, sub: str) -> int:
    """A users row for tests that drive the queue directly, below the HTTP layer."""
    return await db.insert(
        "INSERT INTO users (oidc_sub, email, display_name, created_at) VALUES (?, ?, ?, 1)",
        (sub, f"{sub}@example.com", sub))


@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


@pytest.fixture
def tmp_output_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class FakeComfy:
    """Scriptable stand-in for ComfyUI.

    Emits the same WebSocket sequence a real 0.30 server does -- including
    `progress_state` rather than the flat `progress` message the spec
    describes -- so the relay is exercised exactly as it will run.

    `mode` picks the script: ok | error | interrupt | hang | reject.
    """

    def __init__(self):
        self.base_url = "http://fake-comfy"
        self.client_id = "test-client"
        self.mode = "ok"
        self.steps = 4
        self.delay = 0.01
        self.images = [{"filename": "out.png", "subfolder": "", "type": "output"}]

        self.submitted: list[tuple[str, dict]] = []
        self.interrupts: list[str] = []
        self.drops: list[str] = []
        self.uploaded: list[tuple[str, int]] = []
        self.freed = 0

        self._events: asyncio.Queue = asyncio.Queue()
        self._stopped: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    # -- plumbing ----------------------------------------------------------
    async def health(self):
        return self.mode != "down"

    async def aclose(self):
        for t in self._tasks:
            t.cancel()

    async def _emit(self, event: dict):
        await self._events.put(event)

    async def watch(self):
        yield {"type": "_ws_connected", "data": {}}
        while True:
            yield await self._events.get()

    # -- the API the queue actually uses ------------------------------------
    async def submit(self, graph: dict, prompt_id: str):
        from alchemyui.comfy import ComfyError

        self.submitted.append((prompt_id, graph))
        if self.mode == "reject":
            raise ComfyError(
                "KSampler: sampler_name -- value not in list",
                detail='{"node_errors": {"3": {}}}',
                node_errors={"3": {"class_type": "KSampler", "errors": []}},
            )
        if self.mode != "hang":
            t = asyncio.create_task(self._drive(prompt_id))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return {"prompt_id": prompt_id, "number": 1}

    async def _drive(self, pid: str):
        await self._emit({"type": "execution_start", "data": {"prompt_id": pid}})

        if self.mode == "error":
            await asyncio.sleep(self.delay)
            await self._emit({"type": "execution_error", "data": {
                "prompt_id": pid, "node_id": "3", "node_type": "KSampler",
                "exception_message": "Expected all tensors to be on the same device",
                "exception_type": "RuntimeError",
                "traceback": ["  File \"nodes.py\", line 1, in sample"],
            }})
            return

        for i in range(1, self.steps + 1):
            await asyncio.sleep(self.delay)
            if pid in self._stopped:
                await self._emit({"type": "execution_interrupted", "data": {"prompt_id": pid}})
                return
            await self._emit({"type": "progress_state", "data": {
                "prompt_id": pid,
                "nodes": {"3": {"value": i, "max": self.steps, "state": "running",
                                "node_id": "3", "prompt_id": pid}},
            }})

        await self._emit({"type": "executing", "data": {"prompt_id": pid, "node": None}})

    async def upload_image(self, data: bytes, name: str, subfolder: str = "alchemyui") -> str:
        self.uploaded.append((name, len(data)))
        return f"{subfolder}/{name}" if subfolder else name

    async def interrupt(self, prompt_id: str) -> bool:
        self.interrupts.append(prompt_id)
        self._stopped.add(prompt_id)
        return True

    async def drop_pending(self, prompt_id: str) -> bool:
        self.drops.append(prompt_id)
        return True

    async def free(self, unload_models: bool = True) -> bool:
        self.freed += 1
        return True

    async def history(self, prompt_id: str) -> dict:
        return {"outputs": {"9": {"images": list(self.images)}}}

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        return TINY_PNG


def _wire(monkeypatch, db_path: str, output_dir: str, env: dict) -> tuple[Database, FakeComfy]:
    """Point the app at a temp database, a temp output directory and a scripted
    ComfyUI -- but the real, shipped registry and template, so the workflow
    under test is the one that actually ships."""
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("OUTPUT_DIR", output_dir)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()

    db = Database(db_path)
    comfy = FakeComfy()
    reg = load_registry(REPO / "workflows")
    # lifespan() and the queue reach these by name; routes reach them via Depends
    for mod in (appmod, queuemod):
        monkeypatch.setattr(mod, "get_db", lambda: db)
        monkeypatch.setattr(mod, "get_comfy", lambda: comfy)
        monkeypatch.setattr(mod, "get_registry", lambda: reg)
    appmod.app.dependency_overrides[get_db] = lambda: db
    appmod.app.dependency_overrides[get_comfy] = lambda: comfy
    appmod.app.dependency_overrides[get_registry] = lambda: reg
    return db, comfy


@pytest.fixture
def client(tmp_db_path, tmp_output_dir, monkeypatch, request):
    marker = request.node.get_closest_marker("env")
    db, comfy = _wire(monkeypatch, tmp_db_path, tmp_output_dir,
                      marker.kwargs if marker else {})
    with TestClient(appmod.app) as c:
        c.comfy = comfy
        c.db = db
        yield c
    appmod.app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def live_server(tmp_db_path, tmp_output_dir, monkeypatch):
    """The same app behind a real socket.

    TestClient cannot be used for /api/events: httpx's ASGI transport buffers
    a response to completion before handing it back, and an SSE stream never
    completes. The event stream is the spine of this app, so it gets tested
    over a real connection rather than not at all. Still no GPU and no ComfyUI.
    """
    db, comfy = _wire(monkeypatch, tmp_db_path, tmp_output_dir, {})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(
        appmod.app, log_level="warning", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not come up"

    try:
        yield f"http://127.0.0.1:{port}", comfy, db
    finally:
        server.should_exit = True
        thread.join(15)
        appmod.app.dependency_overrides.clear()
        get_settings.cache_clear()


def _wait(client, predicate, timeout=10.0):
    """Poll an endpoint until it says what we're waiting for. The queue runs
    on the app's own loop, so the test watches it from the outside."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the queue")


def wait_for(client, predicate, timeout=10.0):
    """Poll until a predicate says what we're waiting for. The queue runs on
    the app's own loop, so tests watch it from the outside."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the queue")
