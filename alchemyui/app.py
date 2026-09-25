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
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .comfy import ComfyClient, get_comfy
from .config import Settings, get_settings
from .db import Database, get_db, now
from .faces import FaceRejected, get_analyser, normalise_reference
from .identity import Principal, get_principal
from .queue import QueueManager, get_queue, start_queue, stop_queue
from .registry import MAX_SEED, Registry, get_registry
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
    return {"email": principal.email, "display_name": principal.display_name,
            "is_admin": principal.is_admin}


@app.get("/api/workflows")
async def list_workflows(principal: Principal = Depends(get_principal),
                         reg: Registry = Depends(get_registry),
                         qm: QueueManager = Depends(get_queue)):
    out = []
    for w in reg.all():
        entry = w.public()
        entry["checkpoints"] = [c.public() for c in reg.checkpoints_for(w)] or [
            {"name": w.checkpoint, "label": w.checkpoint, "family": "sdxl",
             "blurb": "", "aspects": entry.get("aspects", {}), "defaults": {}}
        ]
        # Per checkpoint, because a swap and a step cost wildly different
        # amounts depending on which model is in play.
        timings = {}
        for c in entry["checkpoints"]:
            name = c["name"]
            cold = name != qm._resident_checkpoint
            steps = int(c["defaults"].get("steps") or w.defaults.get("steps") or 25)
            sps, load = (qm._stats.get(qm._stats_key(w.key, name))
                         or qm._seed_for(w.key, name))
            timings[name] = {
                "resident": not cold,
                "est_seconds": round(qm.estimate(w.key, steps, cold, name)),
                "seconds_per_step": round(sps, 2),
                "load_seconds": round(load, 1),
            }
        entry["timings"] = timings
        here = timings.get(w.checkpoint) or next(iter(timings.values()))
        entry["resident"] = here["resident"]
        entry["est_seconds"] = here["est_seconds"]
        entry["seconds_per_step"] = here["seconds_per_step"]
        entry["load_seconds"] = here["load_seconds"]
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


@app.post("/api/images/{image_id}/reuse")
async def reuse_image(image_id: str,
                      principal: Principal = Depends(get_principal),
                      settings: Settings = Depends(get_settings),
                      db: Database = Depends(get_db),
                      comfy: ComfyClient = Depends(get_comfy)):
    """Turn a generated image back into an editable source.

    The gallery lives in OUTPUT_DIR and ComfyUI's LoadImage can only see its
    own input/, so an image has to make that trip before it can be enhanced,
    transformed or painted over. Going through the same normalisation as a
    fresh upload means one code path, and one set of guarantees about size.
    """
    row = await db.fetch_one("SELECT path FROM images WHERE id = ?", (image_id,))
    if row is None:
        raise HTTPException(404, "no such image")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(410, "the file for this image is gone from disk")
    return await _record_upload(db, comfy, settings, path.read_bytes())


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
# reference faces (Phase 4) -- validated on arrival, never at generation time
# --------------------------------------------------------------------------- #
def _ref_row(r) -> dict:
    return {
        "id": r["id"], "label": r["label"], "width": r["width"], "height": r["height"],
        "faces_found": r["faces_found"], "face_score": r["face_score"],
        "face_px": r["face_px"], "created_at": r["created_at"],
        "url": f"/api/references/{r['id']}/file",
    }


@app.get("/api/references")
async def list_references(principal: Principal = Depends(get_principal),
                          db: Database = Depends(get_db)):
    rows = await db.fetch_all("SELECT * FROM refs ORDER BY created_at DESC LIMIT 100")
    return {"references": [_ref_row(r) for r in rows]}


@app.post("/api/references")
async def create_reference(file: UploadFile = File(...),
                           principal: Principal = Depends(get_principal),
                           settings: Settings = Depends(get_settings),
                           db: Database = Depends(get_db),
                           comfy: ComfyClient = Depends(get_comfy)):
    """Validate and normalise one reference face.

    This is the whole point of the phase's error handling: InsightFace kills
    the entire batch if any one image has no detectable face, so each is
    checked here, on its own, and refused by name with something actionable.
    Nothing reaches a generation unless every reference is already known good
    and already the same size.
    """
    label = (file.filename or "that image").strip()[:80]
    try:
        png, meta = normalise_reference(
            await file.read(), settings, get_analyser(settings), label)
    except FaceRejected as e:
        raise HTTPException(400, str(e))

    ref_id = uuid.uuid4().hex
    path = save_upload(settings.upload_dir, png, f"ref-{ref_id}")
    comfy_name = await comfy.upload_image(png, f"ref-{ref_id}.png")
    ts = now()
    await db.execute(
        "INSERT INTO refs (id, path, comfy_name, label, width, height, faces_found, "
        "  face_score, face_px, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ref_id, path, comfy_name, label, meta["width"], meta["height"],
         meta["faces_found"], meta["face_score"], meta["face_px"], ts),
    )
    row = await db.fetch_one("SELECT * FROM refs WHERE id = ?", (ref_id,))
    return _ref_row(row)


@app.get("/api/references/{ref_id}/file")
async def reference_file(ref_id: str,
                         principal: Principal = Depends(get_principal),
                         db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT path FROM refs WHERE id = ?", (ref_id,))
    if row is None:
        raise HTTPException(404, "no such reference")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(410, "the file for this reference is gone from disk")
    return FileResponse(path, media_type="image/png")


@app.delete("/api/references/{ref_id}")
async def delete_reference(ref_id: str,
                           principal: Principal = Depends(get_principal),
                           db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT path FROM refs WHERE id = ?", (ref_id,))
    if row is None:
        raise HTTPException(404, "no such reference")
    await db.execute("DELETE FROM refs WHERE id = ?", (ref_id,))
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except OSError:
        log.warning("failed to remove reference file %s", row["path"])
    return {"deleted": ref_id}


@app.get("/api/loras")
async def list_loras(principal: Principal = Depends(get_principal),
                     comfy: ComfyClient = Depends(get_comfy)):
    """Personal LoRAs the likeness workflow can apply.

    FaceID's own LoRAs are filtered out: IPAdapterUnifiedLoaderFaceID already
    applies the matching one itself, and loading it a second time here would
    have it fighting its own weights.
    """
    try:
        names = await comfy.loras()
    except Exception:  # noqa: BLE001 -- an empty picker beats a broken page
        log.warning("could not list loras", exc_info=True)
        names = []
    return {"loras": [n for n in names if "ip-adapter-faceid" not in n.lower()]}


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
    # Phase 3 -- the quality chain
    scale_by: float | None = None
    refine: float | None = None
    refine_steps: int | None = None
    face_denoise: float | None = None
    face_guide: int | None = None
    # Phase 4 -- likeness
    reference_ids: list[str] | None = None
    face_weight: float | None = None
    face_weight_v2: float | None = None
    face_start: float | None = None
    face_end: float | None = None
    lora_name: str | None = None
    lora_strength: float | None = None
    # Phase 5
    checkpoint: str | None = None
    batch: int = 1


@app.post("/api/generate")
async def generate(req: GenerateRequest,
                   principal: Principal = Depends(get_principal),
                   settings: Settings = Depends(get_settings),
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
    # Checked by name, not by "requires is non-empty": a likeness workflow
    # requires references and no image at all, and would otherwise be told to
    # supply one.
    if "image" in workflow.requires:
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

    if "references" in workflow.requires:
        ids = req.reference_ids or []
        if not ids:
            raise HTTPException(400, "add at least one reference photo of the face")
        limit = workflow.references.max if workflow.references else len(ids)
        if len(ids) > limit:
            raise HTTPException(400, f"at most {limit} reference photos")
        rows = []
        for ref_id in ids:
            row = await db.fetch_one("SELECT * FROM refs WHERE id = ?", (ref_id,))
            if row is None:
                raise HTTPException(404, f"reference {ref_id} is no longer available")
            rows.append(row)
        raw["references"] = [r["comfy_name"] for r in rows]
        # A personal LoRA and FaceID both encode identity, and at full strength
        # they fight. Unless a weight was chosen explicitly, start lower.
        if req.lora_name and req.face_weight is None:
            raw["face_weight"] = settings.lora_face_weight

    chosen = None
    if req.checkpoint:
        chosen = reg.checkpoint(req.checkpoint)
        if chosen is None:
            raise HTTPException(400, f"unknown model: {req.checkpoint}")
        if chosen.family not in workflow.families:
            raise HTTPException(
                400, f"{chosen.label} is a {chosen.family} model, which "
                     f"{workflow.display} cannot use")
    else:
        chosen = reg.checkpoint(workflow.checkpoint)

    params = workflow.resolve(raw, chosen)
    if not params["prompt"]:
        raise HTTPException(400, "a prompt is required")

    if "scale_by" in workflow.params:
        # The chain's output edge is 4 x source x scale_by. Rather than let a
        # large source OOM the sampler, reduce the scaling to fit -- and record
        # what was actually used, so the params on the image stay truthful.
        src_w = params.get("width") or (upload["width"] if upload else 0)
        src_h = params.get("height") or (upload["height"] if upload else 0)
        longest = max(src_w, src_h)
        if longest:
            ceiling = settings.hq_max_edge / (4 * longest)
            if params["scale_by"] > ceiling:
                params["scale_by"] = round(max(0.05, ceiling), 4)
                log.info("job scale_by reduced to %.4f to hold %dpx from a %dpx source",
                         params["scale_by"], settings.hq_max_edge, longest)
            params["hq_output_edge"] = round(4 * longest * params["scale_by"])
    if workflow.kind == "outpaint" and not any(
            params.get(f"pad_{s}") for s in ("left", "top", "right", "bottom")):
        raise HTTPException(400, "choose at least one direction to extend into")

    # Batch generation is n separate jobs, not one graph with batch_size=n:
    # they queue, report and cancel individually, and a 6GB card cannot hold
    # a batched SDXL latent anyway.
    count = max(1, min(req.batch, settings.max_batch))
    explicit_seed = req.seed is not None
    jobs = []
    for i in range(count):
        p = dict(params)
        if i and not explicit_seed:
            p = workflow.resolve({**raw, "seed": None}, chosen)
        elif i:
            # An explicit seed still has to vary, or every image is identical.
            p["seed"] = (p["seed"] + i) % (MAX_SEED + 1)
            for extra in ("hq_seed", "face_seed"):
                if extra in p:
                    p[extra] = p["seed"]
        job = await qm.submit(workflow, p)
        if upload is not None:
            await db.execute(
                "UPDATE jobs SET input_image_path = ?, mask_path = ? WHERE id = ?",
                (upload["path"], upload["mask_path"], job.id),
            )
        jobs.append(job)

    first = jobs[0]
    return {"job_id": first.id, "position": qm.position_of(first.id),
            "params": first.params,
            "job_ids": [j.id for j in jobs], "batch": count}


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


@app.get("/api/jobs/{job_id}/preview")
async def job_preview(job_id: str,
                      principal: Principal = Depends(get_principal),
                      qm: QueueManager = Depends(get_queue)):
    """The latest live preview frame for a running job.

    Served over HTTP rather than pushed down the SSE stream: a frame is tens of
    kilobytes and arrives several times a second, which would swamp an event
    stream that also carries queue state. The stream just says a newer frame
    exists and the browser fetches it at whatever rate it can keep up with.
    """
    job = qm._by_id.get(job_id)
    if job is None or job.preview is None:
        raise HTTPException(404, "no preview available")
    return Response(content=job.preview, media_type=job.preview_media,
                    headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# gallery
# --------------------------------------------------------------------------- #
def _image_row(r) -> dict:
    return {
        "id": r["id"], "job_id": r["job_id"], "width": r["width"], "height": r["height"],
        "seed": r["seed"], "workflow_id": r["workflow_id"],
        "params": json.loads(r["params_json"]), "favorite": bool(r["favorite"]),
        "tags": [t for t in (r["tags"] or "").split(",") if t],
        "created_at": r["created_at"], "url": f"/api/images/{r['id']}/file",
    }


def _clean_tags(raw: list[str]) -> str:
    seen = []
    for t in raw:
        t = " ".join(str(t).lower().split())[:32]
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen[:12])


@app.get("/api/images")
async def list_images(limit: int = 100, before: float | None = None,
                      q: str = "", favorite: bool = False, tag: str = "",
                      workflow_id: str = "",
                      principal: Principal = Depends(get_principal),
                      db: Database = Depends(get_db)):
    """The gallery, newest first, with the filters the UI offers.

    Prompt search runs against params_json rather than a separate index: every
    image already stores its full parameter set, so there is nothing to keep
    in sync and a LIKE over a few thousand rows is instant.
    """
    limit = max(1, min(limit, 500))
    where, args = [], []
    if before is not None:
        where.append("created_at < ?")
        args.append(before)
    if q.strip():
        where.append("LOWER(params_json) LIKE ?")
        args.append(f"%{q.strip().lower()}%")
    if favorite:
        where.append("favorite = 1")
    if tag.strip():
        where.append("(',' || tags || ',') LIKE ?")
        args.append(f"%,{tag.strip().lower()},%")
    if workflow_id.strip():
        where.append("workflow_id = ?")
        args.append(workflow_id.strip())

    sql = "SELECT * FROM images"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return {"images": [_image_row(r) for r in await db.fetch_all(sql, args)]}


class ImageUpdate(BaseModel):
    favorite: bool | None = None
    tags: list[str] | None = None


@app.patch("/api/images/{image_id}")
async def update_image(image_id: str, req: ImageUpdate,
                       principal: Principal = Depends(get_principal),
                       db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT * FROM images WHERE id = ?", (image_id,))
    if row is None:
        raise HTTPException(404, "no such image")
    if req.favorite is not None:
        await db.execute("UPDATE images SET favorite = ? WHERE id = ?",
                         (int(req.favorite), image_id))
    if req.tags is not None:
        await db.execute("UPDATE images SET tags = ? WHERE id = ?",
                         (_clean_tags(req.tags), image_id))
    return _image_row(await db.fetch_one("SELECT * FROM images WHERE id = ?", (image_id,)))


@app.get("/api/tags")
async def list_tags(principal: Principal = Depends(get_principal),
                    db: Database = Depends(get_db)):
    counts: dict[str, int] = {}
    for r in await db.fetch_all("SELECT tags FROM images WHERE tags != ''"):
        for t in (r["tags"] or "").split(","):
            if t:
                counts[t] = counts.get(t, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"tag": t, "count": n} for t, n in ordered]}


@app.get("/api/history")
async def prompt_history(q: str = "", limit: int = 40,
                         principal: Principal = Depends(get_principal),
                         db: Database = Depends(get_db)):
    """Distinct prompts actually used, most recent first.

    Read back out of the images table rather than kept separately: the whole
    point of storing the parameter set on every image was that it becomes the
    history, the reproducibility trail and the search index at once.
    """
    limit = max(1, min(limit, 200))
    rows = await db.fetch_all(
        "SELECT params_json, workflow_id, MAX(created_at) AS at, COUNT(*) AS n "
        "FROM images GROUP BY json_extract(params_json, '$.prompt') "
        "ORDER BY at DESC LIMIT 400"
    )
    needle = q.strip().lower()
    out = []
    for r in rows:
        params = json.loads(r["params_json"])
        prompt = (params.get("prompt") or "").strip()
        if not prompt or (needle and needle not in prompt.lower()):
            continue
        out.append({"prompt": prompt, "workflow_id": r["workflow_id"],
                    "used": r["n"], "at": r["at"], "params": params})
        if len(out) >= limit:
            break
    return {"history": out}


# --------------------------------------------------------------------------- #
# presets -- a named workflow + parameter bundle
# --------------------------------------------------------------------------- #
class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    workflow_id: str
    params: dict


def _preset_row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "workflow_id": r["workflow_id"],
            "params": json.loads(r["params_json"]), "created_at": r["created_at"],
            "updated_at": r["updated_at"]}


@app.get("/api/presets")
async def list_presets(principal: Principal = Depends(get_principal),
                       db: Database = Depends(get_db)):
    rows = await db.fetch_all("SELECT * FROM presets ORDER BY name")
    return {"presets": [_preset_row(r) for r in rows]}


@app.post("/api/presets")
async def save_preset(req: PresetIn,
                      principal: Principal = Depends(get_principal),
                      reg: Registry = Depends(get_registry),
                      db: Database = Depends(get_db)):
    if reg.get(req.workflow_id) is None:
        raise HTTPException(400, f"unknown workflow: {req.workflow_id}")
    name = " ".join(req.name.split())
    ts = now()
    existing = await db.fetch_one("SELECT id FROM presets WHERE name = ?", (name,))
    preset_id = existing["id"] if existing else uuid.uuid4().hex
    # Saving over a name updates it rather than erroring: renaming to reuse a
    # name is a chore, and there is one user.
    await db.execute(
        "INSERT INTO presets (id, name, workflow_id, params_json, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "  workflow_id=excluded.workflow_id, params_json=excluded.params_json, "
        "  updated_at=excluded.updated_at",
        (preset_id, name, req.workflow_id, json.dumps(req.params), ts, ts),
    )
    return _preset_row(await db.fetch_one("SELECT * FROM presets WHERE id = ?", (preset_id,)))


@app.delete("/api/presets/{preset_id}")
async def delete_preset(preset_id: str,
                        principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    row = await db.fetch_one("SELECT id FROM presets WHERE id = ?", (preset_id,))
    if row is None:
        raise HTTPException(404, "no such preset")
    await db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
    return {"deleted": preset_id}


# --------------------------------------------------------------------------- #
# gallery
# --------------------------------------------------------------------------- #


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
