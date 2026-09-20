# AlchemyUI — Project Spec

> Hand this file to Claude Code as the project brief. Read it fully before writing code.
> Build in phases. Do not start Phase 2 until Phase 1 runs end to end.

A self-hosted web app for AI image generation and editing, backed by a local
ComfyUI instance. Sibling project to **Llamacracy** (the author's LLM chat hub)
and deliberately built the same way.

---

## 1. Goal and shape

Single prompt box, optional image upload, clean results gallery. The target
feel is ChatGPT's image generation. The user never sees a node graph.

Single user (possibly a few friends later), one GPU, strict FIFO queue. No
multi-tenancy, no cloud, no payment, no scaling. Build clean seams so those
*could* be added; do not build them.

**Name:** AlchemyUI.

---

## 2. Stack — match Llamacracy

This is deliberate. The author has already built and shipped Llamacracy with
this stack, and knows it. Do not introduce a JS framework or a build step.

- **Backend:** FastAPI, Python 3.14+, `uv` for dependency management
- **Database:** one SQLite connection (WAL) behind one asyncio lock. Writes are
  tiny and the queue is serial anyway. Timestamps are epoch floats throughout.
- **Frontend:** vanilla JS, no build step. One `app.js`, one hand-written
  `styles.css`. Any libraries come from cdnjs as classic scripts with a
  graceful fallback if the CDN is blocked.
- **Browser transport:** **SSE** for progress and job events, matching
  Llamacracy's streaming pattern. The app talks WebSocket *to ComfyUI*, but
  relays to the browser over SSE.
- **Process management:** systemd **user** units with linger, idempotent
  `install.sh`, an `alchemyui {up,down,restart,status,logs}` CLI wrapper —
  same pattern as Llamacracy's four units.
- **uvicorn:** run with `--timeout-graceful-shutdown 5`. An open SSE connection
  never closes on its own; without the bound the unit hangs on SIGTERM.

### Patterns to lift directly from Llamacracy

Read that codebase before designing these; do not reinvent them.

- `queue.py` — single worker task draining a FIFO, cancellation that really
  aborts the upstream request, state in SQLite, learned time estimates (EWMA
  over observed durations). The structure maps almost 1:1; substitute
  "stream tokens from llama-swap" with "relay progress from ComfyUI".
- The identity approach (§9).
- The declarative-inventory → generated-registry idea, applied here to
  workflows rather than models.
- systemd unit layout, install script, CLI wrapper.

### What NOT to carry over

- **No metering, credits, sessions or limits.** Single user; there is no
  fairness problem to solve. Keep per-job timing on the job row for honest
  ETAs, but no billing, no cost model, no `MAX_*` caps.
- No model picker complexity beyond a simple checkpoint list.
- No OpenAI-compatible API surface.

---

## 3. Environment (verified — do not guess)

**ComfyUI host** — currently the laptop
- Path: `~/ComfyUI`, server `http://127.0.0.1:8188`
- ComfyUI 0.30.0, Python 3.14.7, PyTorch 2.13.0+cu130
- Launch flag in use: `--fp8_e4m3fn-unet`
- Custom nodes: `ComfyUI_IPAdapter_plus`, `comfyui-impact-pack`,
  `comfyui-impact-subpack`, `ComfyUI-Manager`

**Hardware (laptop)**
- GTX 1660 Ti Mobile, 6GB VRAM (Turing, no tensor cores)
- i7-9750H, 40GB RAM (ComfyUI pins ~35GB)
- Known quirk: GTX 16-series has unreliable fp16 in some paths. Black or NaN
  output is a precision issue, not an app bug. Surface it; do not retry silently.

**Models present**
- `RealVisXL_V5.0_fp16.safetensors` (SDXL, primary)
- `epicrealism_naturalSinRC1VAE.safetensors` (SD1.5)
- IPAdapter FaceID PlusV2 (SD1.5) + LoRA; CLIP Vision ViT-H; InsightFace `buffalo_l`
- `4xNMKDSuperscale_4xNMKDSuperscale.pt`, `bbox/face_yolov8m.pt`

**Performance — design the UX around these**
- SDXL 1024², 25 steps: ~60s
- SD1.5 512×768, 25 steps: ~15s
- Checkpoint swap: 30s+
- Full chain (base + hires + FaceDetailer): 2–4 min

### Deployment question to resolve with the author before Phase 3

Llamacracy runs on a separate box (GTX 1080 Ti, 11GB, reachable over NetBird).
ComfyUI currently runs on the laptop. **If AlchemyUI is ever pointed at the
1080 Ti, it will contend for VRAM with Llamacracy's resident model.** 11GB does
not hold a 7B GGUF and SDXL at once.

If that becomes the plan, the fix is co-residency control: have AlchemyUI call
Llamacracy's llama-swap unload endpoint before a generation and let the idle
TTL do the rest — effectively extending Llamacracy's existing "one model
resident" rule across both apps. Do not build this speculatively. Ask first.

---

## 4. Architecture

```
 browser ──SSE/HTTP──▶ [oauth2-proxy] ──▶ FastAPI (AlchemyUI) ──WS + HTTP──▶ ComfyUI :8188
                                              │   ▲
                                              ▼   │
                                        SQLite (WAL)          outputs/ on disk
                                       jobs · images
```

**Hard rule:** never expose port 8188 to the browser. ComfyUI has no auth and
`/prompt` accepts arbitrary graphs — arbitrary node execution and filesystem
access. All traffic proxies through the app.

---

## 5. The workflow registry — the core design decision

Get this right first. Users never touch workflows. The app keeps a library of
pre-built ComfyUI workflows exported in **API format**, deep-clones one per
request, and injects parameters into known node IDs before POSTing.

**API format vs UI format:** export with **Save (API Format)**. The regular
save produces a UI graph with node positions, link arrays and
`widgets_values` — submitting that to `/prompt` fails validation. API format is
a flat map:

```json
{ "3": { "class_type": "KSampler",
         "inputs": { "seed": 0, "steps": 25, "model": ["4", 0] } } }
```

### Declarative registry, mirroring `bench/inventory.json`

A capability is a registry entry, not code. Adding a feature means: build the
graph in ComfyUI, export API format, add an entry. No backend changes.

```json
"txt2img-sdxl": {
  "display": "Generate (SDXL)",
  "template": "sdxl_txt2img.v1.api.json",
  "checkpoint": "RealVisXL_V5.0_fp16.safetensors",
  "blurb": "Photorealistic, 1024px native.",
  "inputs": ["prompt", "negative", "aspect", "seed", "steps", "cfg"],
  "params": {
    "prompt":   { "node": "5", "field": "text" },
    "negative": { "node": "6", "field": "text" },
    "seed":     { "node": "8", "field": "seed" },
    "steps":    { "node": "8", "field": "steps" },
    "cfg":      { "node": "8", "field": "cfg" }
  },
  "output_node": "10",
  "est": { "seconds_per_step": 2.4, "load_s": 35 }
}
```

`est` seeds the ETA until real runs are observed, exactly like Llamacracy's
`seed` block. Guess generously; nothing breaks if it is off.

**Node IDs are brittle** — they change if a graph is rebuilt in the editor. So:
- Version every template (`sdxl_txt2img.v1.api.json`)
- On startup, validate every registry entry against its template: each bound
  node ID must exist and expose the named input field. **Fail loudly at boot**,
  not at generation time.
- Ship `alchemyui validate` as a standalone check.

---

## 6. ComfyUI API contract

**Submit**
```
POST /prompt   { "prompt": <api_json>, "client_id": "<uuid>" }
→ { "prompt_id": "...", "number": 3 }
→ on failure: { "error": {...}, "node_errors": {...} }
```
Always read `node_errors` — it names the exact node and field. Log it verbatim;
map to a friendly message for the UI.

**Upload input image**
```
POST /upload/image   (multipart: image, overwrite=true, subfolder optional)
→ { "name": "...", "subfolder": "", "type": "input" }
```
Use the returned `name` in the LoadImage node's `image` field.

**Progress** — `ws://127.0.0.1:8188/ws?clientId=<uuid>`. Handle:
- `status` — queue remaining
- `execution_start`
- `executing` — `{node, prompt_id}`; `node: null` means this prompt finished
- `progress` — `{value, max}` sampler steps; drives the progress bar
- `executed` — `{node, output}`, contains output filenames
- `execution_cached` — nodes skipped
- `execution_error` — full traceback; capture it
- binary frames — live preview images when enabled

**Retrieve**
```
GET /history/<prompt_id>
GET /view?filename=<f>&subfolder=<s>&type=output
```
Do not poll `/history` while the WS is connected. WS for progress, `/history`
once on completion.

**Cancel**
```
POST /interrupt                          — abort the running job
POST /queue  {"delete": ["<prompt_id>"]} — drop a pending job
```

Where an API detail is uncertain, read `script_examples/websockets_api_example.py`
and the `@routes` in `server.py` in the ComfyUI repo. Do not guess.

---

## 7. Queue

Single GPU, strictly serial. The app keeps its **own** FIFO and submits one job
at a time to ComfyUI — ComfyUI's internal queue is not the source of truth.

States: `queued → running → completed | failed | cancelled`

- Show real queue position, not a spinner. "2 ahead of you" is a different
  experience from "loading."
- Persist the queue; a backend restart must not lose pending jobs.
- On startup, reconcile: anything left `running` becomes `failed` (ComfyUI
  restarted, unrecoverable).
- Learn durations per workflow (EWMA over observed runs), seeded from `est`.
  Show an honest ETA, never a fake one.
- **Checkpoint-swap awareness:** sort pending jobs so those sharing a
  checkpoint run consecutively — a swap costs 30s+. Cap the reordering window
  so nothing starves.
- Cancelling a running job calls `/interrupt` and really aborts it.
- Read the resident checkpoint from ComfyUI rather than guessing, so the
  "cold start ~35s" hint is honest. (Llamacracy does this via `/running`;
  find the ComfyUI equivalent or track it from the last completed job.)

---

## 8. Data model

```sql
jobs(
  id, workflow_id, status, prompt_id, queue_position,
  params_json, input_image_path, mask_path, error_text,
  created_at, started_at, finished_at
)

images(
  id, job_id, filename, path, width, height,
  seed, favorite, created_at
)
```

Store the full parameter set on every image. That enables "reuse these
settings", "vary this seed", and a reproducibility trail. Cheap now, expensive
to retrofit.

Copy outputs out of ComfyUI's `output/` into app-owned storage. Do not assume
ComfyUI's folder layout is stable.

---

## 9. Identity

Single user by default: run bound to `127.0.0.1`, `DEV_MODE=1` equivalent, no
auth code in the app.

For friends over NetBird, reuse Llamacracy's chain rather than building
anything: **register a second OAuth client in the existing Dex**
(`deploy/dex/config.yaml`), run a second oauth2-proxy on its own port, and
trust the forwarded headers. Key users on the OIDC `sub`, never the email.
Refuse to serve if the headers are missing and `DEV_MODE` is unset.

Plain HTTP inside the tunnel is deliberate — WireGuard is the encryption layer.
Consequences that shape the frontend: `navigator.clipboard` and service workers
are unavailable (both need a secure context). `<a download>` works fine, so
image downloads are unaffected; a "copy image" feature would need an
`execCommand` fallback.

---

## 10. Phases

### Phase 1 — MVP. Build this completely before anything else.

Text-to-image on SDXL only.

- Prompt box; negative prompt behind a "more options" disclosure
- Aspect ratio: 1:1 (1024×1024), 3:4 (896×1152), 4:3 (1152×896),
  9:16 (768×1344), 16:9 (1344×768)
- Advanced panel, collapsed: steps, cfg, sampler, scheduler, seed + lock toggle
- Submit → queue position → live progress → result
- Gallery, newest first, click to enlarge, download
- Failed jobs show a readable message; the traceback stays available

**Acceptance:** submit from the browser, see accurate step-by-step progress,
get an image, find it in the gallery after a refresh, cancel a running job.

### Phase 2 — Editing

Three separate templates, not one.

- **img2img** — VAE Encode the upload; denoise slider 0.3–0.9 labelled in plain
  language ("subtle" → "complete reimagining")
- **Inpainting** — canvas mask editor: brush size, eraser, undo, clear. Export
  a PNG mask matching source dimensions, upload alongside the image, feed both
  to `VAEEncodeForInpaint`. **This is the hardest frontend piece in the
  project** — plain Canvas API, no framework needed, but budget real time.
- **Outpainting** — `ImagePadForOutpaint` ahead of the inpaint path, with
  directional pad controls

### Phase 3 — Quality chain

Optional "high quality" toggle exposing the author's tuned workflow:
`ImageUpscaleWithModel` (4xNMKD) → `ImageScaleBy` 0.38 → `VAEEncodeTiled`
(tile 512, overlap 64) → KSampler @ denoise 0.35 → `VAEDecodeTiled` →
FaceDetailer (`bbox/face_yolov8m.pt`, guide_size 512–768, denoise 0.35–0.45).

Warn in the UI that this roughly triples generation time.

### Phase 4 — Identity / likeness

- IPAdapter FaceID: reference upload, weight + weight_faceidv2 sliders
- Multi-reference via chained `ImageBatch`
- **Known failure modes, already hit in practice — handle them, don't
  rediscover them:**
  - InsightFace throws `Exception: InsightFace: No face detected.` if *any*
    image in a batch fails detection. The whole job dies.
  - `ImageBatch` rescales and center-crops later images to match the **first**
    image's dimensions, which can slice a face out of frame.
  - **Fix:** normalise every reference upload server-side with PIL to identical
    square dimensions (768×768, face centred) before submitting, and validate
    each upload individually on arrival. Reject a bad image with a clear
    message rather than letting the batch fail.
- Personal LoRA support via `LoraLoader` between checkpoint and IPAdapter.
  When a LoRA is active, default FaceID weight lower (~0.4–0.5) — the two fight
  each other at full strength.

### Phase 5 — Platform

Model picker across SD1.5/SDXL with swap-aware queueing · LoRA library with
thumbnails · prompt history with search · favourites and tags · batch
generation (n images, sequential) · saved presets (named workflow + params
bundle) · ControlNet if VRAM allows · live preview frames from the WS binary
channel.

---

## 11. Frontend notes

- Design for minute-scale waits: progress bar with step count and elapsed time,
  never a bare spinner.
- Default view is one input and one button. Everything else behind disclosure.
- The gallery is the main surface — most time is spent looking at results.
- Optimistic UI: a placeholder card appears the moment a job is queued.
- Keep the SSE connection alive across navigation; reconnect with backoff and
  recover state from the jobs table.
- Composer auto-focus only on devices with a fine pointer, so a phone never
  gets its keyboard summoned by a re-render (a Llamacracy lesson).
- Desktop first; mobile-usable is a bonus.

---

## 12. Non-goals

User accounts and multi-tenancy · cloud or GPU-rental integration · a node
graph editor · model downloading/management UI · video generation · payments
or credits · installing ComfyUI custom nodes from the app.

---

## 13. Deliverables

1. Working app; a documented dev command starts it against a local ComfyUI
2. `workflows/` — API-format templates + `registry.json`
3. `alchemyui validate` startup/standalone workflow check
4. `deploy/` — systemd user units, idempotent `install.sh`, CLI wrapper
5. `README.md` (what it is, quick start) and `docs/DECISIONS.md` (every
   non-obvious choice with its reasoning) — same documentation shape as
   Llamacracy
6. `.env.example` with `COMFYUI_URL`, `OUTPUT_DIR`, `PORT`, `DEV_MODE`
7. `tests/` — pytest covering the queue, registry validation, and HTTP-level
   API behaviour. No GPU needed to run the suite.

---

## 14. Working agreement

- Read the Llamacracy codebase first. Match its conventions, layout and
  documentation style. Where this spec and Llamacracy's existing practice
  disagree, raise it rather than silently picking.
- Build Phase 1 end to end before touching Phase 2.
- Test against the real ComfyUI at 127.0.0.1:8188, not mocks. Real workflows
  fail in ways mocks do not.
- Nothing machine-specific gets committed: models, generated config, secrets
  and hostnames live in gitignored files the installer scaffolds from
  committed examples.