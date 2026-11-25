# AlchemyUI

Self-hosted image generation with a single prompt box, backed by a local
[ComfyUI](https://github.com/comfyanonymous/ComfyUI). You never see a node
graph. Sibling project to [**Llamacracy**](https://github.com/jdankney/Llamacracy),
built the same way on purpose: FastAPI, one SQLite file, vanilla JS with no
build step, systemd user units.

One GPU, strict FIFO, honest ETAs, no cloud.

You type, optionally attach images, and get images back in a conversation.
No modes or samplers to set. The one choice beyond words is which model paints:
**Auto**, or a photoreal model pinned from the pill in the prompt box.

- *"Generate me a photo of a spooky lighthouse at night"* makes one.
- *"make it darker"*, *"give me 2 variations"*, *"use the second one"* work
  on what the conversation just made.
- *[a photo]* *"make this look like an oil painting"*, *"sharpen this up"*,
  *"make it wider"* edit what you attach.
- *"Me as an astronaut"* uses the likeness photos saved in your settings.
- Anything it cannot do yet, like *"add me into this photo of my bed"*, gets an
  honest question, with what it can do instead.

A small language model turns each message into a **plan** in a closed
vocabulary, never a graph; code checks the plan and turns it into ComfyUI
jobs. The plan is always shown on the reply as one line ("Generating ·
Realistic Vision 6.0 · 3:4"). Tap it for the rewritten prompt, model and
seed, and to run it again.

A few friends share one GPU: accounts through Llamacracy's Dex, everything
private to its owner, strict FIFO across people, energy and electricity cost
per person, and a live admin page.

> **Status: proof of concept, no longer under active development.** It runs
> daily on one laptop for a handful of people, and is published as it stands.
> Phases 6 (multi-user), 7 (the chat and its router), 7.5 (the Alchemy UI, the
> model switcher and the Photoreal Lab), 8 (editing one part of a photo) and
> 9 (web references, "me in Nike Pro sweatpants"; needs a SearXNG URL) are in.
> See [Roadmap](#roadmap) for what was planned next.
>
> It assumes a setup like the author's: an NVIDIA GPU with ComfyUI on the same
> machine, and, for more than one user, [NetBird](https://netbird.io) plus a
> Dex instance (Llamacracy's) for sign-in. Single-user local use needs neither.

Underneath the chat, everything v1 built is still there: SDXL and SD1.5
generation, img2img, inpainting on a dedicated inpainting UNet, outpainting,
the enhance chain (4x upscale, tiled refine, face cleanup), FaceID likeness,
live previews, a queue with real positions and learned ETAs, and a library
with favourites and search.

---

## Quick start

You need a working ComfyUI (with its own Python environment) and
[uv](https://github.com/astral-sh/uv).

```bash
# 1. ComfyUI, in its own terminal
cd ~/ComfyUI && ./venv/bin/python main.py --fp8_e4m3fn-unet --preview-method auto

# 2. AlchemyUI
git clone https://github.com/jdankney/AlchemyUI.git && cd AlchemyUI
cp .env.example .env          # defaults are fine for a local single-user run
uv sync
uv run alchemyui validate --live    # is the registry sane? are the models there?
uv run uvicorn alchemyui.app:app --host 127.0.0.1 --port 8300 --timeout-graceful-shutdown 5
```

Open <http://127.0.0.1:8300>.

`--preview-method auto` is what makes live previews appear while a job
samples; ComfyUI defaults to none, and without it everything else still works.

`--timeout-graceful-shutdown 5` is not optional in practice: an open SSE
connection never closes on its own, so without the bound the process hangs on
SIGTERM until something kills it.

### Models

Models are listed in the `checkpoints` block of `workflows/registry.json`,
each with its own native resolutions and sampling defaults — SD1.5 and SDXL
want very different numbers. A workflow declares which families its template
suits, and the picker only offers models it can actually run.

Checkpoints live in family folders under `~/ComfyUI/models/checkpoints/`, and
the registry names them the way ComfyUI does, folder included:

| Family | Checkpoints |
|---|---|
| `SDXL/` | RealVis XL 5.0 (the default for generation) |
| `SD1.5/` | Realistic Vision 6.0, Realistic Vision Hyper (six steps), epiCRealism, SD 1.5 base |
| `SD1.5-Inpaint/` | Realistic Vision 6.0 Inpainting (the default for paint-over and extend), and its Hyper variant |

Each carries `traits` (`fast`, `quality`, `photoreal`, `inpaint`, `draft`)
that the router's model policy chooses on: a request for "best" quality gets
RealVis XL, anything else a `fast` SD1.5 model (preferring whichever is
already loaded), extending gets the inpainting UNet. The Hyper models are
`draft`: quick, but they watermark, so nothing picks them automatically. To add one, drop it into its family folder
and add an entry to the `checkpoints` block of `workflows/registry.json` — no
code changes. Old bare filenames from v1 presets still resolve.

`alchemyui validate --live` tells you whether the checkpoint is actually
present before you wait through a generation to find out.

**Picking one.** The pill in the prompt box lists the photoreal SDXL and SD1.5
models, each with a one-line `tagline` from the registry, whether it is loaded,
and how long an image takes right now. A pick sticks to the thread. The
policy honours it, except where that would break the picture:
- extending always uses the inpainting UNet;
- likeness and enhance need SDXL;
- a source under 900px is not handed to SDXL.

When a pick is not used, a note under the reply says why.

**Profiles.** A model's tuned defaults are steps, cfg, sampler, scheduler, a
*look* appended to every prompt, and a negative prompt. They are adopted from
the Photoreal Lab and stored in the database, laid over the registry's
defaults for everyone until reset. The registry file stays the baseline.

Paint-over and extend default to the dedicated inpainting UNet, whose
nine-channel input sees the unmasked image while it samples. That is what
removes the faint seam v1 showed when it inpainted with RealVisXL.

The quality chain additionally needs `4xNMKDSuperscale_4xNMKDSuperscale.pt`
in `models/upscale_models/` and `bbox/face_yolov8m.pt` in
`models/ultralytics/bbox/`, plus the Impact Pack custom nodes.
Likeness needs `ip-adapter-faceid-plusv2_sdxl.bin` in `models/ipadapter/`, its
paired LoRA in `models/loras/`, `CLIP-ViT-H-14` in `models/clip_vision/`, and
the ComfyUI_IPAdapter_plus nodes. `alchemyui validate --live` checks all of it.

AlchemyUI also reads InsightFace's `buffalo_l` models directly, to check
reference faces as they are uploaded — set `INSIGHTFACE_ROOT` if ComfyUI is
not at `~/ComfyUI`.

Region edits add two more requirements:

- **CLIPSeg for changing something that is already there.** Install the
  `ComfyUI-CLIPSeg` custom node (the maintained `zhengxyz123` fork; the
  original repo is gone) and let it download `CIDAS/clipseg-rd64-refined` on
  first use. The app draws the mask itself for a box placement, so only the
  text-region path needs this node.
- **IPAdapter Plus for web references.** `ip-adapter-plus_sd15.bin` and
  `ip-adapter-plus_sdxl_vit-h.bin` in `models/ipadapter/`. The existing
  `CLIP-ViT-H-14` and FaceID LoRAs are reused.

`alchemyui validate --live` names any of these that are missing.

---

### The router

Each message is planned by a small vision LLM, off the GPU, while other
people's jobs run:

1. **Llamacracy** (`ROUTER_PRIMARY_URL`, `ROUTER_PRIMARY_KEY`,
   `ROUTER_PRIMARY_MODEL`): its `/v1` as a dedicated `alchemyui` account.
2. **A CPU-only llama-server on this laptop** (`alchemyui-router`), when
   Llamacracy is down, slow, or its model cannot see the attached images
   (Llamacracy's `/v1` routes images to each model's `-vision` entry).
3. **Your own words**, when neither answers: text becomes the prompt as-is;
   an attached image gets a short "restyle, enhance or extend?" question.

The chip says which one planned a turn, and the admin page lists every turn
Llamacracy did not plan. Output is constrained to a JSON schema generated from
the registry and from the images *you* can name in that conversation, so a
model cannot pick an action that doesn't exist or reach anyone else's image.

```bash
uv run alchemyui router-eval              # 37 cases, both backends, side by side
uv run alchemyui router-eval --backend primary --only ex3-me-into-my-bed
```

The report lands in `data/router-eval/`. It is how the primary model was
chosen, and what to rerun after changing the prompt.

### Editing one part of a photo (Phase 8)

- **"Change the sky"** becomes `edit_region` with a phrase. CLIPSeg turns
  that phrase into a mask, and the SD1.5 inpainting UNet repaints only there.
- **"Add me to my bed"** becomes a normalized box. The app draws a soft
  mask for the box, and optionally adds FaceID from the attachment that
  shows the person's face. The box comes from the router's vision model.
- Both paths run on the dedicated inpainting checkpoint and use the same
  queue, GPU metering and cancellation as every other job.

### Web references (Phase 9)

Set `SEARCH_URL` to a SearXNG instance with the JSON API enabled. Then:

1. "Me in Nike Pro sweatpants" becomes `with_reference` with a short image
   search phrase.
2. The app sends only that phrase to SearXNG and parks the turn with a grid
   of candidates. Nothing is auto-picked.
3. You pick one; the app downloads it with an SSRF-safe fetcher (public
   addresses only, redirects re-checked, size cap, Pillow decode/re-encode),
   and applies it with IPAdapter Plus. FaceID keeps your face, and the
   reference carries the garment or object.
4. With a photo attached, the reference is applied as an img2img edit; with
   no photo, it makes a new picture of your saved likeness wearing it.

The search endpoint is trusted config, but every result image is untrusted
input. The browser never receives the remote bytes through the app; it sees
only the candidate thumbnails.

---

### The Photoreal Lab

`/lab`, for admins, is where the models are tuned towards photographs.

1. **Define a run.** Choose one to four models, some of the eight committed
   test prompts (`workflows/lab.json`: portrait, street, landscape,
   interior, food, animal, night city, hands) or your own, and one to three
   seeds. Then choose up to two things to compare: cfg, steps, sampler, look
   or negative. The form shows the image count and roughly how long it will
   take before you start.
2. **It renders in the background.** Lab jobs run in a low-priority lane.
   Anyone's message goes ahead of every Lab job, and takes the GPU from a
   running one with a targeted interrupt. That cell is simply rendered again
   later. People's queue positions never count Lab work.
3. **Compare and star.** Every cell of a run shares its seed, so what differs
   between neighbours is the setting. The grid is laid out as rows × columns,
   per model and prompt. Star what reads most like a photograph.
4. **Make default.** The tally adds stars up across every prompt and seed.
   Making the winner the default saves only what differs from the registry,
   and makes that image the model's cover in the menu. *Model defaults*
   shows what each model runs with, marks what the Lab changed, and can
   **Reset** it or copy it as registry JSON to commit.

---

## Running it as a service

```bash
./deploy/install.sh        # systemd user units + linger + the alchemyui-ctl CLI
alchemyui-ctl status       # unit state, plus whether the app, ComfyUI and the router answer
alchemyui-ctl logs
```

Idempotent — safe to re-run after every `git pull`. It does not manage ComfyUI;
that has its own flags and its own lifetime. The router fallback is installed
once `.env` names its model (`ROUTER_FALLBACK_GGUF`, `ROUTER_FALLBACK_MMPROJ`);
it runs with no GPU visible to it at all, and needs about 7.5GB of RAM.

---

## Accounts and the admin page

Locally, `DEV_MODE=1` gives you one fake identity and no login. To share over
NetBird, `deploy/install.sh` sets up an oauth2-proxy on this machine's NetBird
interface that authenticates against Llamacracy's Dex: it prints the client
entry to add to Dex's `config.yaml` on the other box. Then unset `DEV_MODE`.
Without `DEV_MODE`, the app refuses to serve any request that lacks
oauth2-proxy's identity headers, so it can never run open by accident.

- **Everyone's work is their own.** Conversations, images, jobs, uploads,
  likeness photos and the live event stream are scoped to their owner. Someone else's
  item is a 404, not a 403.
- **Strict FIFO across users.** The queue still groups one person's jobs by
  model to avoid a 30s checkpoint swap, but it never moves anyone ahead of
  someone else.
- **`ADMIN_EMAILS`** (comma-separated) names who may open `/admin`: the GPU
  live (watts, temperature, VRAM, utilisation), the queue with each job's
  owner, energy and cost per user over 7 days, 30 days or all time, recent
  failures, turns planned without Llamacracy, and disabling an account. It
  shows **metadata only**: never anyone's messages, prompts or images.
- **Energy** is the GPU's measured draw (sampled with `nvidia-smi` while a job
  holds it) plus `NON_GPU_LOAD_WATTS` for the rest of the machine, times the
  time it held the GPU. Cost uses `ELECTRICITY_RATE`, or `TOU_SCHEDULE` for
  time-of-use rates, and is frozen on the job. It is for visibility; nothing
  is ever blocked on it.
- **The first admin to sign in inherits everything v1 made.**
  `uv run alchemyui reassign FROM_EMAIL TO_EMAIL` moves an account's rows
  elsewhere. Use it when you switch from the dev identity to your real Dex
  login, or when someone's Dex account is recreated.

---

## How it works

```
 browser ──SSE/HTTP──▶ FastAPI (AlchemyUI) ──HTTP + WebSocket──▶ ComfyUI :8188
                            │   │
                            │   └─ planner ─▶ Llamacracy /v1  (fallback: llama-server :8081, CPU)
                            ▼
                      SQLite (WAL)            data/outputs · data/uploads
                      threads · turns · jobs · images
```

A message becomes a **turn**. The turn is planned (`planning`), may stop to ask
you something (`needs_input`, holding no place in the queue), then queues its
jobs, one ComfyUI run each.

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
uv run pytest -q          # 382 tests: no GPU, no ComfyUI, no LLM; node for the front-end boot tests
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
                  threads.py turns and their states · chat.py /api/threads, /api/turns
                  planner/ the router: schema, policy, prompt, client, router
                  jobs.py plan -> queued jobs · registry.py workflows
                  storage.py files+uploads · faces.py likeness validation
                  identity.py users · admin.py /api/admin · power.py nvidia-smi
                  meter.py energy and cost · search.py SearXNG + safe reference fetch
                  evals.py router-eval
                  profiles.py tuned model defaults · lab.py, lab_api.py the Photoreal Lab
                  cli.py validate, reassign, router-eval
alchemyui/static/ index.html, admin.html, lab.html · assets/ app.js, admin.js, lab.js, styles.css
                  assets/fonts/ Sora and JetBrains Mono (OFL), self-hosted: no CDN at all
workflows/        registry.json (workflows + checkpoints) + seven templates · lab.json (the Lab's test prompts)
evals/router/     router-eval's 37 cases and their fixture photos (generated here)
deploy/           app, auth-edge and router units, oauth2-proxy config, install.sh, alchemyui-ctl
docs/             DECISIONS.md -- every non-obvious choice, with its reasoning
tests/            pytest; no GPU required
```

`docs/DECISIONS.md` is the one to read before changing anything structural.

---

## Roadmap

v2, in phases. Each one is built and verified end to end before the next
starts. The details are in [`ALCHEMYUI_SPEC.md`](ALCHEMYUI_SPEC.md) §14.

| Phase | What it adds |
|---|---|
| 1–5 | **v1, complete.** Generate, edit, quality chain, likeness, and the platform layer. |
| 6 | **Multi-user platform. Done.** Dex accounts via oauth2-proxy, everything scoped per user, strict FIFO across users, energy and cost per job, and a live admin page. The fixed model catalogue and the SD1.5 inpaint models also land here. |
| 7 | **Chat UI and router. Done.** One prompt box with attachments, threads, and a planner LLM (Llamacracy, with a CPU-only local fallback) that turns each message into a validated plan. |
| 7.5 | **Alchemy UI, model choice, Photoreal Lab. Done.** A new look with images as the star, a per-message model switcher, and an admin Lab that benchmarks settings and adopts the winners as per-model defaults. |
| 8 | **Region-aware editing. Done.** "Add me to my bed", "change the sky": text segmentation or a box mask, then inpaint. |
| 9 | **Web references. Code and models in; needs `SEARCH_URL`.** "Me in Nike Pro sweatpants": search, pick, fetch safely, apply with IPAdapter Plus. |

The design principle that holds it together: **the LLM writes a plan in a
closed vocabulary, never a graph.** The registry turns the plan into a
ComfyUI graph and validates it exactly as it does today.

---

## What this is not

A node graph editor, billing or usage limits, an OpenAI-compatible API, model
downloading, cloud GPUs, video. See `ALCHEMYUI_SPEC.md` §15.
