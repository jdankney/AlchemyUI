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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .comfy import ComfyClient, get_comfy
from .config import get_settings
from .db import Database, get_db
from .identity import Principal, get_principal
from .queue import QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry

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


@app.post("/api/generate")
async def generate(req: GenerateRequest,
                   principal: Principal = Depends(get_principal),
                   reg: Registry = Depends(get_registry),
                   qm: QueueManager = Depends(get_queue)):
    """Enqueue and return immediately.

    Unlike Llamacracy's chat endpoint this does not stream: an image job takes
    minutes, outlives any one request, and the user is expected to navigate
    away and come back. Progress arrives on the single /api/events stream.
    """
    workflow = reg.get(req.workflow_id)
    if workflow is None:
        raise HTTPException(400, f"unknown workflow: {req.workflow_id}")

    params = workflow.resolve(req.model_dump(exclude_none=True))
    if not params["prompt"]:
        raise HTTPException(400, "a prompt is required")

    job = await qm.submit(workflow, params)
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
