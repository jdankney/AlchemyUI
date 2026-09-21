# AlchemyUI

Self-hosted image generation with a single prompt box, backed by a local
[ComfyUI](https://github.com/comfyanonymous/ComfyUI). You never see a node
graph. Sibling project to **Llamacracy**, built the same way on purpose:
FastAPI, one SQLite file, vanilla JS with no build step, systemd user units.

One GPU, strict FIFO, honest ETAs. No accounts, no metering, no cloud.

**What works today:**

- **Generate** — text to image on SDXL: prompt, negative prompt, aspect ratio,
  sampler settings, live step-by-step progress, cancel, and a gallery that
  survives a refresh.
- **Transform** — img2img with a plain-language "how much should change?"
  slider instead of a denoise number.
- **Paint over** — inpainting, with a brush/eraser mask editor (undo, clear,
  adjustable brush) drawn straight onto the image.
- **Extend** — outpainting in any direction, with a to-scale preview of the
  canvas you are about to ask for.
- **Enhance** — the quality chain: 4× upscale, scale back, a tiled hi-res
  refine pass, then face cleanup. Works on any image you already have,
  including one you just inpainted. Roughly triples the wait.
- **High quality toggle** on Generate — the same chain welded onto a fresh
  generation, as one job. Measured at about 2.8× a plain generation on a
  GTX 1660 Ti (372s against 133s).

- **Likeness** — generate someone from reference photos of their face
  (IPAdapter FaceID), up to four at once. Each photo is checked for a face and
  cropped square around it as you add it, so a bad one is refused there rather
  than killing a job minutes later.

Any image in the gallery can go straight back into the editors: **Edit** drops
it into Transform, **Enhance** runs the quality chain over it.

---

## Quick start

You need a working ComfyUI (with its own Python environment) and
[uv](https://github.com/astral-sh/uv).

```bash
# 1. ComfyUI, in its own terminal
cd ~/ComfyUI && ./venv/bin/python main.py --fp8_e4m3fn-unet

# 2. AlchemyUI
cd ~/Documents/Coding/AlchemyUI
cp .env.example .env          # defaults are fine for a local single-user run
uv sync
uv run alchemyui validate --live    # is the registry sane? are the models there?
uv run uvicorn alchemyui.app:app --host 127.0.0.1 --port 8300 --timeout-graceful-shutdown 5
```

Open <http://127.0.0.1:8300>.

`--timeout-graceful-shutdown 5` is not optional in practice: an open SSE
connection never closes on its own, so without the bound the process hangs on
SIGTERM until something kills it.

### Models

All four workflows point at `RealVisXL_V5.0_fp16.safetensors` in
`~/ComfyUI/models/checkpoints/`. To use a different SDXL checkpoint, change the
`checkpoint` field in `workflows/registry.json` — no code changes.

`alchemyui validate --live` tells you whether the checkpoint is actually
present before you wait through a generation to find out.

Editing uses the same checkpoint as generation. RealVisXL is not an
*inpainting* checkpoint, so inpainted regions can show a faint seam; the
"mask feather" slider is what trades that against how much of the original
is preserved.

The quality chain additionally needs `4xNMKDSuperscale_4xNMKDSuperscale.pt`
in `models/upscale_models/` and `bbox/face_yolov8m.pt` in
`models/ultralytics/bbox/`, plus the Impact Pack custom nodes.
Likeness needs `ip-adapter-faceid-plusv2_sdxl.bin` in `models/ipadapter/`, its
paired LoRA in `models/loras/`, `CLIP-ViT-H-14` in `models/clip_vision/`, and
the ComfyUI_IPAdapter_plus nodes. `alchemyui validate --live` checks all of it.

AlchemyUI also reads InsightFace's `buffalo_l` models directly, to check
reference faces as they are uploaded — set `INSIGHTFACE_ROOT` if ComfyUI is
not at `~/ComfyUI`.

---

## Running it as a service

```bash
./deploy/install.sh        # systemd user unit + linger + the alchemyui-ctl CLI
alchemyui-ctl status       # unit state, plus whether the app and ComfyUI answer
alchemyui-ctl logs
```

Idempotent — safe to re-run after every `git pull`. It does not manage ComfyUI;
that has its own flags and its own lifetime.

---

## How it works

```
 browser ──SSE/HTTP──▶ FastAPI (AlchemyUI) ──HTTP + WebSocket──▶ ComfyUI :8188
                            │   ▲
                            ▼   │
                      SQLite (WAL)            OUTPUT_DIR on disk
                      jobs · images
```

**Port 8188 is never exposed to a browser.** ComfyUI has no auth and `/prompt`
accepts arbitrary graphs — that is arbitrary node execution and filesystem
access. Everything, including finished images, is served by the app.

### The workflow registry

A capability is a registry entry, not code. Adding one means: build the graph in
ComfyUI, export it with **Save (API Format)** into `workflows/`, add an entry to
`workflows/registry.json`, restart.

```json
"txt2img-sdxl": {
  "display": "Generate (SDXL)",
  "template": "sdxl_txt2img.v1.api.json",
  "checkpoint": "RealVisXL_V5.0_fp16.safetensors",
  "params": { "prompt": { "node": "6", "field": "text" }, ... },
  "output_node": "9",
  "est": { "seconds_per_step": 5.25, "load_s": 35 }
}
```

The app deep-clones the template per request and injects parameters into the
bound node IDs. Node IDs are brittle — rebuilding a graph in the editor
renumbers them — so **every binding is checked against its template at boot**
and a mismatch is a loud startup failure, never a surprise at generation time.

```bash
uv run alchemyui validate          # registry <-> templates (offline, no GPU)
uv run alchemyui validate --live   # + node classes and checkpoints on a live ComfyUI
```

### The queue

Strict FIFO, one generation at a time. The app keeps its **own** queue and
submits a single prompt to ComfyUI at a time, so it can report real positions
("2 ahead of you"), cancel for real, and survive a restart with pending work
intact. Durations are learned per workflow (EWMA over observed runs, seeded
from `est`), so the ETA is measured rather than invented.

---

## Development

```bash
uv run pytest -q          # 120 tests, no GPU and no ComfyUI needed
uv run ruff check .
```

The suite uses a scripted `FakeComfy` that emits the same WebSocket sequence a
real ComfyUI 0.30 does. The SSE endpoint is tested over a real socket, because
httpx's ASGI transport buffers a response to completion and an event stream
never completes.

---

## Layout

```
alchemyui/        app.py routes+SSE · queue.py the FIFO · comfy.py the client
                  registry.py workflows · storage.py files+uploads
                  faces.py reference validation · cli.py validate
workflows/        registry.json + seven API-format templates
deploy/           systemd user unit, idempotent install.sh, alchemyui-ctl
docs/             DECISIONS.md -- every non-obvious choice, with its reasoning
tests/            pytest; no GPU required
```

`docs/DECISIONS.md` is the one to read before changing anything structural.

---

## What this is not

User accounts, multi-tenancy, credits or metering, a node graph editor, model
downloading, cloud GPUs, video. See `ALCHEMYUI_SPEC.md` §12.
