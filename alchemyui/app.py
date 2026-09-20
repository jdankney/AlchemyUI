"""FastAPI application. Binds 127.0.0.1 only. Serves the SPA and the JSON/SSE
API, and is the only thing that ever talks to ComfyUI.

Hard rule (spec section 4): port 8188 is never reachable from a browser.
ComfyUI has no auth and /prompt accepts arbitrary graphs -- that is arbitrary
node execution and filesystem access. Even finished images are served from
app-owned storage rather than proxied from /view.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .comfy import ComfyClient, get_comfy
from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal
from .queue import QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry
from .storage import UploadRejected, normalise_mask, normalise_upload, save_upload

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("alchemyui")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_db()          # applies the schema
    get_registry()    # validates every entry against its template; SystemExit if not

    comfy = get_comfy()
    if not await comfy.health():
        log.warning("ComfyUI not reachable at %s -- the queue will keep retrying",
                    comfy.base_url)

    Path(settings.output_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    await start_queue()
    log.info("alchemyui up on %s:%s (dev_mode=%s)",
             settings.app_bind_host, settings.port, settings.is_dev)
    try:
        yield
    finally:
        await stop_queue()
        await comfy.aclose()
        get_db().close()


app = FastAPI(title="AlchemyUI", lifespan=lifespan)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# --------------------------------------------------------------------------- #
# health / capability
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz(comfy: ComfyClient = Depends(get_comfy)):
    return {"ok": True, "comfyui": await comfy.health()}


@app.get("/api/me")
async def me(principal: Principal = Depends(get_principal)):
    return {"email": principal.email, "display_name": principal.display_name}


@app.get("/api/workflows")
async def list_workflows(principal: Principal = Depends(get_principal),
                         reg: Registry = Depends(get_registry),
                         qm: QueueManager = Depends(get_queue)):
    out = []
    for w in reg.all():
        entry = w.public()
        cold = w.checkpoint != qm._resident_checkpoint
        entry["resident"] = not cold
        # An honest headline number for the default step count, so the UI can
        # say "about a minute" before anything is submitted.
        entry["est_seconds"] = round(
            qm.estimate(w.key, int(w.defaults.get("steps", 25)), cold)
        )
        entry["seconds_per_step"] = round(qm._stats.get(w.key, [2.0, 30.0])[0], 2)
        entry["load_seconds"] = round(qm._stats.get(w.key, [2.0, 30.0])[1], 1)
        out.append(entry)
    return {"workflows": out, "resident_checkpoint": qm._resident_checkpoint}


# --------------------------------------------------------------------------- #
# uploads (Phase 2) -- the source image, and the mask painted over it
# --------------------------------------------------------------------------- #
async def _record_upload(db: Database, comfy: ComfyClient, settings: Settings,
                         data: bytes) -> dict:
    """Normalise an uploaded image, keep our own copy, and put it where
    ComfyUI's LoadImage can find it."""
    try:
        png, width, height = normalise_upload(
            data, settings.upload_max_edge, settings.upload_max_mb)
    except UploadRejected as e:
        raise HTTPException(400, str(e))

    upload_id = uuid.uuid4().hex
    path = save_upload(settings.upload_dir, png, upload_id)
    comfy_name = await comfy.upload_image(png, f"{upload_id}.png")
    await db.execute(
        "INSERT INTO uploads (id, path, comfy_name, width, height, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (upload_id, path, comfy_name, width, height, now()),
    )
    return {"id": upload_id, "width": width, "height": height,
            "url": f"/api/uploads/{upload_id}/file"}


@app.post("/api/uploads")
async def create_upload(file: UploadFile = File(...),
                        principal: Principal = Depends(get_principal),
                        settings: Settings = Depends(get_settings),
                        db: Database = Depends(get_db),
                        comfy: ComfyClient = Depends(get_comfy)):
    return await _record_upload(db, comfy, settings, await file.read())


@app.post("/api/uploads/{upload_id}/mask")
async def attach_mask(upload_id: str,
                      file: UploadFile = File(...),
                      principal: Principal = Depends(get_principal),
                      settings: Settings = Depends(get_settings),
                      db: Database = Depends(get_db),
                      comfy: ComfyClient = Depends(get_comfy)):
    """Attach a painted mask to an existing upload.

    Scoped to the upload rather than standalone so the mask can be checked
    against the exact dimensions of the image it was painted over -- a mask
    that doesn't line up inpaints the wrong region, silently.
    """
    row = await db.fetch_one("SELECT * FROM uploads WHERE id = ?", (upload_id,))
    if row is None:
        raise HTTPException(404, "no such upload")
    try:
        png = normalise_mask(await file.read(), row["width"], row["height"])
    except UploadRejected as e:
        raise HTTPException(400, str(e))

    path = save_upload(settings.upload_dir, png, f"{upload_id}.mask")
    comfy_name = await comfy.upload_image(png, f"{upload_id}.mask.png")
    await db.execute(
        "UPDATE uploads SET mask_path = ?, mask_comfy_name = ? WHERE id = ?",
        (path, comfy_name, upload_id),
    )
    return {"id": upload_id, "mask": True, "width": row["width"], "height": row["height"]}


@app.get("/api/uploads/{upload_id}/file")
async def upload_file(upload_id: str, mask: bool = False,
                      principal: Principal = Depends(get_principal),
                      db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT path, mask_path FROM uploads WHERE id = ?", (upload_id,))
    if row is None:
        raise HTTPException(404, "no such upload")
    path = Path(row["mask_path"] or "") if mask else Path(row["path"])
    if not path.name or not path.exists():
        raise HTTPException(410, "the file for this upload is gone from disk")
    return FileResponse(path, media_type="image/png")


# --------------------------------------------------------------------------- #
# generation
# --------------------------------------------------------------------------- #
class GenerateRequest(BaseModel):
    workflow_id: str
    prompt: str = Field(min_length=1)
    negative: str | None = None
    aspect: str | None = None
    seed: int | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    # Phase 2. `image_id` refers to an /api/uploads row; the graph never sees
    # a path the browser chose, only the ComfyUI filename we recorded.
    image_id: str | None = None
    denoise: float | None = None
    grow_mask: int | None = None
    pad_left: int | None = None
    pad_top: int | None = None
    pad_right: int | None = None
    pad_bottom: int | None = None
    feathering: int | None = None


@app.post("/api/generate")
async def generate(req: GenerateRequest,
                   principal: Principal = Depends(get_principal),
                   reg: Registry = Depends(get_registry),
                   db: Database = Depends(get_db),
                   qm: QueueManager = Depends(get_queue)):
    """Enqueue and return immediately.

    Unlike Llamacracy's chat endpoint this does not stream: an image job takes
    minutes, outlives any one request, and the user is expected to navigate
    away and come back. Progress arrives on the single /api/events stream.
    """
    workflow = reg.get(req.workflow_id)
    if workflow is None:
        raise HTTPException(400, f"unknown workflow: {req.workflow_id}")

    raw = req.model_dump(exclude_none=True)
    upload = None
    if workflow.requires:
        if not req.image_id:
            raise HTTPException(400, f"{workflow.display} needs an image to work from")
        upload = await db.fetch_one("SELECT * FROM uploads WHERE id = ?", (req.image_id,))
        if upload is None:
            raise HTTPException(404, "no such upload")
        raw["image"] = upload["comfy_name"]
        if "mask" in workflow.requires:
            if not upload["mask_comfy_name"]:
                raise HTTPException(400, "brush over the area you want changed first")
            raw["mask"] = upload["mask_comfy_name"]

    params = workflow.resolve(raw)
    if not params["prompt"]:
        raise HTTPException(400, "a prompt is required")
    if workflow.kind == "outpaint" and not any(
            params.get(f"pad_{s}") for s in ("left", "top", "right", "bottom")):
        raise HTTPException(400, "choose at least one direction to extend into")

    job = await qm.submit(workflow, params)
    if upload is not None:
        await db.execute(
            "UPDATE jobs SET input_image_path = ?, mask_path = ? WHERE id = ?",
            (upload["path"], upload["mask_path"], job.id),
        )
    return {"job_id": job.id, "position": qm.position_of(job.id), "params": job.params}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str,
                     principal: Principal = Depends(get_principal),
                     qm: QueueManager = Depends(get_queue)):
    if not await qm.cancel(job_id):
        raise HTTPException(404, "job not found or already finished")
    return {"cancelled": job_id}


@app.get("/api/jobs")
async def list_jobs(limit: int = 50,
                    principal: Principal = Depends(get_principal),
                    db: Database = Depends(get_db),
                    qm: QueueManager = Depends(get_queue)):
    """Recent jobs, newest first -- how the browser recovers state after a
    reconnect without us having to replay the event stream."""
    limit = max(1, min(limit, 200))
    rows = await db.fetch_all(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    etas = qm._etas()
    jobs = []
    for r in rows:
        live = qm._by_id.get(r["id"])
        if live is not None:
            jobs.append(live.public(qm.position_of(r["id"]), etas.get(r["id"])))
            continue
        jobs.append({
            "id": r["id"], "workflow_id": r["workflow_id"], "status": r["status"],
            "position": None, "eta_s": None,
            "progress": {"value": 0, "max": 0},
            "params": json.loads(r["params_json"]),
            "cold_start": False,
            "error_text": r["error_text"], "error_detail": r["error_detail"],
            "images": [], "created_at": r["created_at"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
        })
    return {"jobs": jobs}


# --------------------------------------------------------------------------- #
# gallery
# --------------------------------------------------------------------------- #
def _image_row(r) -> dict:
    return {
        "id": r["id"], "job_id": r["job_id"], "width": r["width"], "height": r["height"],
        "seed": r["seed"], "workflow_id": r["workflow_id"],
        "params": json.loads(r["params_json"]), "favorite": bool(r["favorite"]),
        "created_at": r["created_at"], "url": f"/api/images/{r['id']}/file",
    }


@app.get("/api/images")
async def list_images(limit: int = 100, before: float | None = None,
                      principal: Principal = Depends(get_principal),
                      db: Database = Depends(get_db)):
    limit = max(1, min(limit, 500))
    if before is None:
        rows = await db.fetch_all(
            "SELECT * FROM images ORDER BY created_at DESC LIMIT ?", (limit,))
    else:
        rows = await db.fetch_all(
            "SELECT * FROM images WHERE created_at < ? ORDER BY created_at DESC LIMIT ?",
            (before, limit))
    return {"images": [_image_row(r) for r in rows]}


@app.get("/api/images/{image_id}/file")
async def image_file(image_id: str,
                     principal: Principal = Depends(get_principal),
                     db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT path, filename FROM images WHERE id = ?", (image_id,))
    if row is None:
        raise HTTPException(404, "no such image")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(410, "the file for this image is gone from disk")
    return FileResponse(path, media_type="image/png", filename=row["filename"] or path.name)


@app.delete("/api/images/{image_id}")
async def delete_image(image_id: str,
                       principal: Principal = Depends(get_principal),
                       db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT path FROM images WHERE id = ?", (image_id,))
    if row is None:
        raise HTTPException(404, "no such image")
    await db.execute("DELETE FROM images WHERE id = ?", (image_id,))
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except OSError:
        log.warning("failed to remove image file %s", row["path"])
    return {"deleted": image_id}


# --------------------------------------------------------------------------- #
# the event stream -- one connection for the whole app
# --------------------------------------------------------------------------- #
@app.get("/api/events")
async def events(request: Request,
                 principal: Principal = Depends(get_principal),
                 qm: QueueManager = Depends(get_queue)):
    """Queue snapshots, per-job progress, state changes and finished images,
    all on one EventSource.

    A long-lived stream rather than Llamacracy's per-request one: image jobs
    outlive the request that created them, and EventSource brings its own
    reconnect backoff. Anything missed while disconnected is recovered from
    /api/jobs and /api/images.
    """
    q = qm.subscribe()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse(event)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            qm.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/queue")
async def queue_now(principal: Principal = Depends(get_principal),
                    qm: QueueManager = Depends(get_queue)):
    return qm.snapshot()


# --------------------------------------------------------------------------- #
# SPA
# --------------------------------------------------------------------------- #
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
              name="assets")

    @app.get("/")
    async def index():
        idx = STATIC_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return {"service": "alchemyui", "ui": "not built"}


def dev() -> None:
    """`uv run alchemyui-dev` -- the documented one-command dev server."""
    import uvicorn

    s = get_settings()
    uvicorn.run("alchemyui.app:app", host=s.app_bind_host, port=s.port,
                reload=True, timeout_graceful_shutdown=5)
