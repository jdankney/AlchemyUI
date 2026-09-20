from __future__ import annotations

import asyncio
import base64
import os
import tempfile

import pytest

os.environ.setdefault("DEV_MODE", "1")

# A real 1x1 PNG, so storage.png_size() has something honest to parse.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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
