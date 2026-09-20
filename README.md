# AlchemyUI

Self-hosted image generation with a single prompt box, backed by a local
[ComfyUI](https://github.com/comfyanonymous/ComfyUI). You never see a node
graph. Sibling project to **Llamacracy**, built the same way on purpose:
FastAPI, one SQLite file, vanilla JS with no build step, systemd user units.

One GPU, strict FIFO, honest ETAs. No accounts, no metering, no cloud.

**Phase 1 (this release):** text-to-image on SDXL — prompt, negative prompt,
aspect ratio, sampler settings, live step-by-step progress, cancel, and a
gallery that survives a refresh.

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

Phase 1 ships one workflow, pointed at `RealVisXL_V5.0_fp16.safetensors` in
`~/ComfyUI/models/checkpoints/`. To use a different SDXL checkpoint, change the
`checkpoint` field in `workflows/registry.json` — no code changes.

`alchemyui validate --live` tells you whether the checkpoint is actually
present before you wait through a generation to find out.

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
uv run pytest -q          # 48 tests, no GPU and no ComfyUI needed
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
                  registry.py workflows · storage.py output files · cli.py validate
workflows/        registry.json + API-format templates
deploy/           systemd user unit, idempotent install.sh, alchemyui-ctl
docs/             DECISIONS.md -- every non-obvious choice, with its reasoning
tests/            pytest; no GPU required
```

`docs/DECISIONS.md` is the one to read before changing anything structural.

---

## What this is not

User accounts, multi-tenancy, credits or metering, a node graph editor, model
downloading, cloud GPUs, video. See `ALCHEMYUI_SPEC.md` §12.
