"""Thin async client for ComfyUI -- HTTP for submit/retrieve, one persistent
WebSocket for progress.

This is the only module that knows ComfyUI's wire format. Port 8188 is never
reachable from a browser: it has no auth and /prompt accepts arbitrary graphs,
which means arbitrary node execution and filesystem access. Everything the UI
needs is relayed through the app.

API details here were read out of the installed ComfyUI (0.30.0) rather than
guessed -- see docs/DECISIONS.md for the two places the wire format has moved
on from what the spec describes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import uuid
from collections.abc import AsyncIterator

import httpx
import websockets

from .config import get_settings

log = logging.getLogger("alchemyui.comfy")


class ComfyError(Exception):
    """A /prompt submission was rejected.

    `node_errors` names the exact node and field and is logged verbatim; the
    UI gets `message`. Never swallow the detail -- on a GTX 16-series card a
    black or NaN output is a precision issue, not an app bug, and the only way
    to tell is to keep what ComfyUI actually said.
    """

    def __init__(self, message: str, detail: str = "", node_errors: dict | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.node_errors = node_errors or {}


def friendly_error(node_errors: dict, fallback: str) -> str:
    """Turn ComfyUI's node_errors into one line a person can act on."""
    for node_id, err in (node_errors or {}).items():
        for e in err.get("errors", []):
            extra = e.get("extra_info") or {}
            field = extra.get("input_name")
            detail = e.get("details") or e.get("message") or "invalid input"
            where = f"{err.get('class_type', 'node ' + str(node_id))}"
            if field:
                return f"{where}: {field} -- {detail}"
            return f"{where}: {detail}"
    return fallback


class ComfyClient:
    def __init__(self, base_url: str | None = None, client_id: str | None = None):
        self.base_url = (base_url or get_settings().comfyui_url).rstrip("/")
        # One client_id for the app's lifetime. ComfyUI routes progress
        # messages to the socket registered under this id, so a single
        # long-lived connection never misses events between jobs.
        self.client_id = client_id or uuid.uuid4().hex
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            # Generous read timeout: /view of a large PNG and /history after a
            # long run are both served off a machine that is busy with a GPU.
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/ws?clientId={self.client_id}"

    # -- health / capability --------------------------------------------------
    async def health(self) -> bool:
        try:
            r = await self._client.get("/system_stats", timeout=5.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def system_stats(self) -> dict:
        r = await self._client.get("/system_stats", timeout=10.0)
        r.raise_for_status()
        return r.json()

    async def checkpoints(self) -> list[str]:
        """Checkpoint filenames ComfyUI can actually load, for `validate --live`."""
        try:
            r = await self._client.get("/models/checkpoints", timeout=10.0)
            if r.status_code == 200:
                return list(r.json())
        except httpx.HTTPError:
            pass
        # Older servers have no /models/{folder}; the node's own input spec
        # carries the same list.
        info = await self.object_info("CheckpointLoaderSimple")
        spec = info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {})
        names = spec.get("ckpt_name", [[]])[0]
        return list(names)

    async def loras(self) -> list[str]:
        """LoRA filenames ComfyUI can load, for the personal-LoRA picker."""
        try:
            r = await self._client.get("/models/loras", timeout=10.0)
            if r.status_code == 200:
                return list(r.json())
        except httpx.HTTPError:
            pass
        info = await self.object_info("LoraLoader")
        spec = info.get("LoraLoader", {}).get("input", {}).get("required", {})
        return list(spec.get("lora_name", [[]])[0])

    async def object_info(self, node_class: str | None = None) -> dict:
        path = f"/object_info/{node_class}" if node_class else "/object_info"
        r = await self._client.get(path, timeout=60.0)
        r.raise_for_status()
        return r.json()

    # -- submit / control -----------------------------------------------------
    async def submit(self, graph: dict, prompt_id: str) -> dict:
        """POST /prompt with a prompt_id we choose.

        ComfyUI 0.30 accepts a client-supplied prompt_id as long as it is a
        canonical lowercase hyphenated UUID (server.py: validate_job_id). That
        removes the race where WebSocket events for this prompt arrive before
        the POST response does -- we can start relaying the moment we submit.
        """
        body = {"prompt": graph, "client_id": self.client_id, "prompt_id": prompt_id}
        try:
            r = await self._client.post("/prompt", json=body, timeout=60.0)
        except httpx.HTTPError as e:
            raise ComfyError("ComfyUI is not reachable", detail=repr(e))

        if r.status_code >= 400:
            try:
                payload = r.json()
            except ValueError:
                raise ComfyError(
                    f"ComfyUI rejected the workflow (HTTP {r.status_code})",
                    detail=r.text[:4000],
                )
            node_errors = payload.get("node_errors") or {}
            err = payload.get("error") or {}
            fallback = err.get("message") or f"ComfyUI rejected the workflow (HTTP {r.status_code})"
            # Logged verbatim: node_errors names the exact node and field, and
            # is the single most useful thing when a template drifts.
            log.error("prompt rejected: %s", json.dumps(payload)[:4000])
            raise ComfyError(
                friendly_error(node_errors, fallback),
                detail=json.dumps(payload, indent=2)[:8000],
                node_errors=node_errors,
            )

        data = r.json()
        # /prompt returns node_errors even on success (non-fatal warnings).
        if data.get("node_errors"):
            log.warning("prompt accepted with node_errors: %s",
                        json.dumps(data["node_errors"])[:2000])
        return data

    async def interrupt(self, prompt_id: str) -> bool:
        """Abort the running prompt -- but only if it is ours.

        ComfyUI 0.30 takes an optional prompt_id and no-ops if that prompt is
        not the one running. A bare /interrupt would kill whatever happens to
        be executing, including work started from ComfyUI's own UI.
        """
        try:
            r = await self._client.post("/interrupt", json={"prompt_id": prompt_id}, timeout=15.0)
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("interrupt(%s) failed: %s", prompt_id, e)
            return False

    async def drop_pending(self, prompt_id: str) -> bool:
        """Remove a not-yet-running prompt from ComfyUI's own queue."""
        try:
            r = await self._client.post("/queue", json={"delete": [prompt_id]}, timeout=15.0)
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("drop_pending(%s) failed: %s", prompt_id, e)
            return False

    async def free(self, unload_models: bool = True) -> bool:
        """Ask ComfyUI to drop resident models and free VRAM."""
        try:
            r = await self._client.post(
                "/free", json={"unload_models": unload_models, "free_memory": True}, timeout=60.0
            )
            return r.status_code < 300
        except httpx.HTTPError as e:
            log.warning("free() failed: %s", e)
            return False

    # -- send an image in (Phase 2) -------------------------------------------
    async def upload_image(self, data: bytes, name: str, subfolder: str = "alchemyui") -> str:
        """Put an image into ComfyUI's input/ and return the name a LoadImage
        node should use.

        `overwrite=true` keeps ComfyUI from inventing "name (1).png" on a
        collision -- our names are uuid-stemmed, so a collision means the same
        upload, and a silently renamed file would break the graph we already
        built. The returned name carries the subfolder because that is what
        LoadImage resolves against.
        """
        files = {"image": (name, data, "image/png")}
        payload = {"overwrite": "true", "type": "input", "subfolder": subfolder}
        try:
            r = await self._client.post("/upload/image", files=files, data=payload, timeout=120.0)
        except httpx.HTTPError as e:
            raise ComfyError("ComfyUI is not reachable", detail=repr(e))
        if r.status_code >= 400:
            raise ComfyError(
                f"ComfyUI rejected the upload (HTTP {r.status_code})", detail=r.text[:2000]
            )
        body = r.json()
        sub = body.get("subfolder") or ""
        return f"{sub}/{body['name']}" if sub else body["name"]

    # -- retrieve -------------------------------------------------------------
    async def history(self, prompt_id: str) -> dict:
        """Read once, on completion. Never polled while the WS is connected."""
        r = await self._client.get(f"/history/{prompt_id}", timeout=30.0)
        r.raise_for_status()
        return r.json().get(prompt_id, {})

    async def view(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        r = await self._client.get(
            "/view", params={"filename": filename, "subfolder": subfolder, "type": type_}
        )
        r.raise_for_status()
        return r.content

    # -- events ---------------------------------------------------------------
    async def watch(self) -> AsyncIterator[dict]:
        """Yield ComfyUI's WebSocket messages forever, reconnecting on drop.

        One socket for the app's lifetime, not one per job: ComfyUI keys
        delivery on clientId, and a persistent connection cannot miss the
        events that land in the gap between two jobs.

        Binary frames are live preview images, decoded here and yielded as a
        `_preview` event. They only arrive at all if ComfyUI was launched with
        --preview-method (it defaults to none), so their absence is normal and
        must never look like a fault.
        """
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=20, max_size=64 * 1024 * 1024
                ) as ws:
                    log.info("comfy ws connected (clientId=%s)", self.client_id)
                    backoff = 1.0
                    yield {"type": "_ws_connected", "data": {}}
                    async for raw in ws:
                        if isinstance(raw, bytes | bytearray):
                            frame = _decode_preview(bytes(raw))
                            if frame is not None:
                                yield {"type": "_preview", "data": frame}
                            continue
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(msg, dict) and "type" in msg:
                            yield msg
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- any socket failure is retryable
                log.warning("comfy ws dropped (%s); retrying in %.0fs", e, backoff)
                yield {"type": "_ws_disconnected", "data": {"error": repr(e)}}
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


# ComfyUI's binary envelope: a big-endian uint32 event type, then the payload.
# For PREVIEW_IMAGE the payload is itself a uint32 image format followed by the
# encoded image (server.py: send_image / encode_bytes, protocol.py).
_BINARY_PREVIEW = 1
_BINARY_PREVIEW_WITH_METADATA = 4
_IMAGE_FORMATS = {1: "image/jpeg", 2: "image/png"}


def _decode_preview(raw: bytes) -> dict | None:
    if len(raw) < 8:
        return None
    event = struct.unpack(">I", raw[:4])[0]
    if event not in (_BINARY_PREVIEW, _BINARY_PREVIEW_WITH_METADATA):
        return None
    media = _IMAGE_FORMATS.get(struct.unpack(">I", raw[4:8])[0])
    if media is None:
        return None
    data = raw[8:]
    return {"media_type": media, "bytes": data} if data else None


_comfy: ComfyClient | None = None


def get_comfy() -> ComfyClient:
    global _comfy
    if _comfy is None:
        _comfy = ComfyClient()
    return _comfy
