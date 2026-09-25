# AlchemyUI — Project Spec (v2)

> Hand this file to Claude Code as the project brief. Read it fully before writing code.
> Build in phases. Do not start a phase until the previous one runs end to end.

A self-hosted, multi-user web app for AI image generation and editing, driven
from a single ChatGPT-style prompt box and backed by a local ComfyUI. Sibling
project to **Llamacracy** (the author's LLM chat hub,
<https://github.com/jdankney/Llamacracy>) and deliberately built the same way.

**What changed from v1.** Phases 1–5 built a single-user tool with a six-mode
composer: Generate, Transform, Paint over, Extend, Enhance, Likeness, each with
its own controls. v2 keeps every workflow underneath and replaces the way you
reach them. There is one prompt box with image attachments, and a small
language model decides which workflow a message needs. The app becomes
multi-user the way Llamacracy is: accounts through Dex, strict FIFO across
users, metering, and an admin page. The v1 brief is in git history
(`git show f3c4a63:ALCHEMYUI_SPEC.md`); `docs/DECISIONS.md` records what it
got right and wrong.

---

## 1. Goal and shape

One prompt box. You type, optionally attach images, and get images back in a
conversation thread. You never choose a mode, a sampler, a model or a denoise
value. You never see a node graph.

All of these should just work from the same box:

1. "Generate me a photo of a spooky lighthouse at night"
2. "Snowy Forest during Snowstorm"
3. *[two images: me, my bed]* "Edit this photo of my bed to add me in it. I
   added myself in the other photo as reference"
4. "Edit me into Nike Pro sweatpants" (the app finds reference photos of the
   garment on the web)
5. Follow-ups in the same thread: "make it darker", "use the second one", "now
   add a dog"

A few friends share one GPU through a strict FIFO queue. Energy use and its
electricity cost are tracked per user, for visibility, not for billing or
limits. An admin page shows the GPU live from anywhere on the NetBird network.

**Name:** AlchemyUI.

---

## 2. Stack — match Llamacracy

Unchanged from v1, and deliberately so. The author has shipped this stack.
Do not introduce a JS framework or a build step.

- **Backend:** FastAPI, Python 3.14+, `uv`.
- **Database:** one SQLite connection (WAL) behind one asyncio lock. Epoch-float
  timestamps throughout.
- **Frontend:** vanilla JS, no build step, no CDN. Hand-written `styles.css`.
- **Browser transport:** SSE, one long-lived stream per tab. WebSocket to
  ComfyUI only.
- **Process management:** systemd **user** units with linger, idempotent
  `install.sh`, the `alchemyui-ctl` wrapper.
- **uvicorn:** `--timeout-graceful-shutdown 5`, always.

### What now carries over from Llamacracy

v1 deliberately left these out. v2 lifts them, near-verbatim, from the files
named:

| Llamacracy | AlchemyUI use |
|---|---|
| `identity.py` | users keyed on OIDC `sub`, `ADMIN_EMAILS`, disabled accounts, the 503 refusal |
| `power.py` | `nvidia-smi` power sampling while a job holds the GPU |
| `metering.py` | the cost formula, and freezing `rate_used` and `cost_usd` on the job row |
| `admin.py` + admin page | live GPU, queue with owners, per-user usage |
| `threads.py` | conversation threads |
| `search.py` | SearXNG querying (Phase 9) |
| `deploy/` | the oauth2-proxy unit and config, Dex client registration |

### What still does not carry over

- **No credits, sessions, weekly caps or limits.** Metering exists to *show*
  energy and cost. It never blocks a job.
- No API keys and no OpenAI-compatible surface. AlchemyUI is a *client* of
  Llamacracy's `/v1`, and does not offer one of its own.
- No invoices, markup or fast lane.

---

## 3. Environment (verified 2026-09-25 — do not guess)

**AlchemyUI + ComfyUI host: the laptop** (`archangel`)
- ComfyUI at `~/ComfyUI`, `http://127.0.0.1:8188`, ComfyUI 0.30.0,
  launched with `--fp8_e4m3fn-unet --preview-method auto`
- Custom nodes: `ComfyUI_IPAdapter_plus`, `comfyui-impact-pack`,
  `comfyui-impact-subpack`, `ComfyUI-Manager`
- GTX 1660 Ti Mobile, 6GB VRAM (Turing, no tensor cores); i7-9750H, 12 threads;
  40GB RAM
- `nvidia-smi --query-gpu=power.draw` **works** (reads ~2.4W at idle). There is
  no cumulative energy counter, so power has to be sampled and integrated.
- Intel RAPL (CPU energy) exists but is **root-only**: that is the standard
  Platypus mitigation. CPU energy is therefore *estimated*.
- `llama-server` 0.4.1 is installed. The distro's `llama-server.service` has
  `ProtectHome=tmpfs` and cannot read `~/models`; AlchemyUI ships its own unit.

**Checkpoints** — organised into family folders, so ComfyUI names them with
the folder prefix (`SD1.5/epicrealism_naturalSinRC1VAE.safetensors`). A
registry that uses bare filenames will not find them.

| Folder | Files |
|---|---|
| `SD1.5/` | `epicrealism_naturalSinRC1VAE`, `realisticVisionV60B1_v60B1VAE_pruned_fp16`, `realisticVisionV60B1_v51HyperVAE_pruned_fp16`, `v1-5-pruned-emaonly` |
| `SD1.5-Inpaint/` | `realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16`, `realisticVisionV60B1_v51HyperInpaintVAE_pruned_fp16` |
| `SDXL/` | `RealVisXL_V5.0_fp16` |
| `SDXL-Inpaint/` | *(empty)* |

**Other models:**
- IPAdapter FaceID PlusV2, SD1.5 and SDXL, each with its paired LoRA
- CLIP-ViT-H-14
- InsightFace `buffalo_l`
- `4xNMKDSuperscale`
- `bbox/face_yolov8m.pt` and `bbox/hand_yolov8s.pt`
- Not present: plain IPAdapter **Plus** (needed for garment and object
  references), any segmentation model, and any ControlNet.

**LLMs**
- **Llamacracy**, on `dex-server` over NetBird (GTX 1080 Ti, 11GB, shared
  with its own users). It serves Qwen3.6-35B-A3B, Gemma4-26B-A4B, Gemma4-E4B
  and others.
  - `/v1/chat/completions` is a **raw passthrough** to llama-server. It
    forwards `response_format` (JSON-schema-constrained output) and
    `image_url` content parts untouched.
  - Each call is queued and metered by Llamacracy under the API key's account.
  - Llamacracy does not persist `/v1` traffic.
  - `/v1` does not say which model is resident, so a router request for a
    model that isn't loaded will trigger a swap on that box.
- **Local fallback:** `~/models/Gemma4-E4B-QAT-UD-Q4_K_XL/`, the GGUF at
  4.2GB plus `mmproj-BF16.gguf`, run **CPU-only** on this laptop.

**Measured performance on this laptop:**

| Job | Time |
|---|---|
| SDXL 1024², 25 steps | ~131s (5.25 s/step) |
| SD1.5 at native resolution | ~32s (~1.2 s/step) |
| Enhance chain | ~248s |
| SDXL high-quality txt2img | ~372s |
| SDXL likeness | ~172s |

A checkpoint swap costs 30s+, cold.

---

## 4. Architecture

```
 browser ─► oauth2-proxy ─► AlchemyUI :8300 ──WS+HTTP──► ComfyUI :8188 (loopback)
            (Dex client on    │   │
             dex-server)    │   ├─ planner ─► Llamacracy /v1 (dex-server)
                              │   │             └► fallback: llama-server :8081, CPU-only
                              │   ├─ search ──► SearXNG                (Phase 9)
                              │   └─ meter ───► nvidia-smi
                              ▼
                        SQLite (WAL) · data/outputs · data/uploads
```

**Hard rules:**
1. **Never expose port 8188.** ComfyUI has no auth, and `/prompt` accepts
   arbitrary graphs: arbitrary node execution and filesystem access. All
   traffic goes through the app, including finished images.
2. **The LLM never writes a graph.** It writes a *plan* in a closed vocabulary
   (§7). Code validates that plan and turns it into a graph through the
   registry. A hallucinating model can only choose among options that exist.
3. **LLM output is untrusted input.** Every image a plan names is a *handle*
   that resolves inside the requesting user's own scope, or the plan is
   rejected.
4. **Nothing machine-specific is committed** (§17).

---

## 5. The workflow registry

Unchanged in role: it is still the executor contract. Pre-built API-format
graphs are deep-cloned per job, parameters are injected into bound node IDs,
and every binding is validated at boot. `alchemyui validate [--live]` runs the
same checks on demand. See v1's `docs/DECISIONS.md` for the reasoning, all of
which still holds.

v2 changes:
- **Checkpoints are referenced by their path under `models/checkpoints/`**,
  folder included.
- **New family `sd15-inpaint`.** The inpaint and outpaint workflows run on it by
  default. The dedicated inpainting UNets remove the seam v1 showed on
  RealVisXL.
- **Each checkpoint gains `traits`** (`fast`, `quality`, `photoreal`,
  `inpaint`) for the model policy (§7) to choose on. The Hyper variants need
  their own few-step sampling defaults; seed them from the model card and
  **measure them live** before trusting them.
- **The registry generates the router's vocabulary.** An action is offered to
  the LLM only if a validated workflow backs it (§7.2).

---

## 6. The turn pipeline

Every message is a **turn** in a **thread**. A turn produces zero or more
**jobs**; a job is one ComfyUI run on the GPU.

```
 submitted ─► planning ─► ready ─► queued ─► running ─► done
                 │          ▲                  │
                 ▼          │                  └─► failed / cancelled
            needs_input ────┘   (the user answers in the thread)
```

**Planning happens off the GPU and concurrently.** The router (§7) runs while
other users' jobs hold the GPU. A turn that needs the user's answer parks in
`needs_input` and **holds no place in the queue**, so nobody ever waits on
another person's reply.

**Run it, or ask?** The plan is always *shown* on the turn as a one-line chip,
e.g. "Generating · Realistic Vision · 3:2". It only *blocks* when the router
would otherwise be guessing:
- **Runs immediately** when nothing is ambiguous: plain text → image, or "make
  it darker" with one obvious target. A wrong guess costs a cancel and a
  correction, which is cheaper than confirming every turn.
- **Stops at `needs_input`** when:
  - which attachment plays which role is unclear;
  - a choice among web results is required;
  - something is missing ("You haven't saved a likeness — attach a photo or
    add one in settings");
  - the request needs a capability that isn't installed (§7.2).

**Queue position is taken when the plan becomes final**, not when the message
was sent. Planning takes seconds, so the order is the same in practice; the
difference only matters for turns waiting on a human.

**"Give me 4 variations"** becomes up to four jobs, queued back to back. The cap
is `MAX_BATCH`, default 4.

**Cancelling a turn** cancels all of its jobs. A queued job is removed; a
running one is interrupted in ComfyUI.

**Failure modes:**
- **Primary LLM unreachable or too slow** (`ROUTER_TIMEOUT_S`) → the local
  fallback.
- **Both LLMs down** → a text-only turn runs as plain txt2img with the user's
  words as the prompt, and says so on the chip. A turn with attachments stops
  at `needs_input` and offers the actions its attachments allow.
- **Invalid plan** (bad JSON, schema violation, or a handle that doesn't
  resolve) → one repair retry that feeds the validation error back, then the
  same fallback.
- **Restart** → turns in `planning` are re-planned; `needs_input` turns keep
  waiting; jobs reconcile as in v1 (an orphaned `running` job becomes
  `failed`, and `queued` jobs are reloaded).

---

## 7. The router

### 7.1 What the router sees and returns

**Input:**
- the user's text;
- thumbnails (≤768px) of this turn's attachments, labelled `a1`, `a2`, …;
- a **text** summary of the thread's recent turns (`ROUTER_CONTEXT_TURNS`,
  default 6): what each one asked, its plan, and the prompt behind each output;
- whether the user has a saved likeness.

Earlier outputs are described by their prompts; their pixels are never sent.
That keeps calls small and fast.

**Output:** one plan, in one of the actions below, with every image named by
a handle:
- `a1`–`a4` — this turn's attachments
- `t4.2` — the second image produced by turn 4 of this thread
- `me` — the user's saved likeness

### 7.2 The vocabulary

| Action | Example | Workflow | Phase |
|---|---|---|---|
| `generate` | "spooky lighthouse at night" | txt2img | 7 |
| `transform` | "make this look like a painting", "make it darker" | img2img | 7 |
| `enhance` | "sharpen this up", "fix the face" | quality chain | 7 |
| `likeness` | "me as an astronaut" | FaceID | 7 |
| `extend` | "make it wider" | outpaint | 7 |
| `edit_region` | "add me to my bed", "change the sky to sunset" | segment → inpaint (+ FaceID / reference) | 8 |
| `with_reference` | "me in Nike Pro sweatpants" | search → pick → `edit_region` | 9 |
| `ask` | the model itself is unsure | → `needs_input` | 7 |

**The vocabulary is generated from the registry.** The JSON schema and the
system prompt are built at boot from the workflows that validated. Until
Phase 8 exists, `edit_region` is not in the schema at all, so "add me to my
bed" can't be routed to something that half-works. Instead it gets an honest
`ask`: "I can't edit part of a photo yet — want me to put you in a bedroom
scene instead?"

### 7.3 The LLM picks meaning; code picks numbers

Plans carry intent words, never tuning values:
- `strength: subtle | medium | strong` — not `denoise`
- `quality: fast | best` — not a checkpoint filename or a step count
- `aspect` from the registry's list; `count` from 1 to `MAX_BATCH`

`registry.py` maps those words onto tuned numbers. `policy.py` chooses the
checkpoint from `traits`, and on `fast` it **prefers the checkpoint already
loaded**, avoiding a swap. The tuning stays in code, where it is measured,
rather than in a model's guess.

The LLM also **rewrites the prompt.** Casual phrasing becomes a proper SD
prompt plus a negative, kept within SD1.5's 77-token window. The thread shows
the user's own words; the rewritten prompt sits behind the plan chip.

### 7.4 Models, constraint, fallback

- **Primary:** Llamacracy `/v1/chat/completions`, authenticated with an API key
  for a dedicated `alchemyui` account. `ROUTER_PRIMARY_MODEL` is configurable.
  Choose a vision model that is usually resident, because a request for any
  other model makes Llamacracy swap and delays its own users.
- **Fallback:** a local `llama-server` running Gemma4-E4B with its mmproj.
  - `-ngl 0` **and** `CUDA_VISIBLE_DEVICES=` are both required. A CUDA build
    can otherwise offload large prompt batches or the vision projector to the
    GPU even with zero layers.
  - It stays resident in about 5GB of RAM.
  - It runs as its own systemd user unit, `alchemyui-router`.
- **Constrained decoding:** `response_format: {type: json_schema, …}`, which
  llama-server enforces as a grammar. Before depending on it, verify it
  survives Llamacracy's passthrough with the real server.
- **Thinking off:** it adds latency and nothing to a classification.
- **Concurrency:** planning is capped per backend: 2 for Llamacracy, 1 for the
  CPU fallback.
- **Every turn records** its `plan_source` (`primary`, `fallback` or `default`),
  router latency and token counts. The admin page surfaces fallbacks.

### 7.5 Evaluation

The router is judged on real models, not on mocks:
- **Unit tests** replay recorded LLM responses.
- **`alchemyui router-eval`** runs a fixture set of 30+ prompts, including
  every example in §1, each with its attachments and expected action, against
  **both** backends. It reports the accuracy side by side. That is how the
  primary model is chosen, and how the gap between primary and fallback is
  known rather than assumed.

---

## 8. Queue

One GPU, strictly serial. The app keeps its own queue and submits one job at a
time to ComfyUI, as in v1.

- **Across users, strictly FIFO.**
- **Swap-aware reordering stays, but only within one user's own jobs.** A job
  may be promoted only past jobs owned by the same user, i.e. within the
  contiguous same-owner run at the head of the queue. It never passes another
  user's job. The v1 window and skip counter still bound it.
- Real positions ("2 ahead of you"), an honest learned ETA, persistence across
  restarts, and cancellation that really aborts: all as v1.
- **A disabled user's queued jobs are cancelled.**

---

## 9. Data model

New tables and columns. Everything else from v1 remains.

```sql
users(id, oidc_sub UNIQUE, email, display_name, is_admin, disabled,
      created_at, last_active_at)

threads(id, user_id, title, created_at, updated_at)

turns(id, thread_id, user_id, seq,            -- seq: per-thread ordinal (the "4" in t4.2)
      text, attachments_json,                -- upload ids, in a1..a4 order
      state, plan_json, plan_source, plan_error, question_json,
      router_ms, prompt_tokens, completion_tokens,
      created_at, planned_at, finished_at)

jobs    += user_id, turn_id, occupancy_seconds, gpu_watts_mean,
           energy_wh, rate_used, cost_usd
images  += user_id, turn_id
uploads += user_id
refs    += user_id, is_likeness
presets += user_id; name unique per user, not globally  -- a table rebuild in SQLite
```

- **Existing single-user rows are claimed by the first admin to sign in.**
  It is logged, and happens once.
- **v1 presets, tags and prompt-history are left in the schema but leave the
  UI.** Threads *are* the history now.

---

## 10. Identity and privacy

- **oauth2-proxy on the laptop** listens on the NetBird interface and
  authenticates against a second OAuth client registered in Llamacracy's Dex.
  AlchemyUI stays on `127.0.0.1` behind it. The deploy artifacts are lifted
  from Llamacracy's `deploy/`.
- **Users are created on first login**, keyed on OIDC `sub`.
  `ADMIN_EMAILS` marks admins, refreshed on every login (Llamacracy's rule).
  A disabled account gets a 403.
- **No identity headers and `DEV_MODE` unset → 503.** The app can never serve
  open. As with Llamacracy, it trusts the forwarded headers because only
  oauth2-proxy should reach 127.0.0.1:8300. That is fine on a single-OS-user
  laptop, and not on a shared machine.
- **Scoping.** Every thread, turn, image, upload, likeness and SSE event belongs
  to one user and is only ever served to that user. The queue snapshot a user
  sees is anonymised: queue depth, and how many are ahead of *you*.
- **The admin page shows metadata, not content:** owner, action, duration,
  energy, errors. It shows no other user's prompts or images. Likeness photos
  make that line important.
- **Likenesses:** 1–4 face photos per user, saved in settings and validated
  with v1's InsightFace pipeline. `me` resolves only to the requester's own
  likeness. No one else's prompt can resolve to your face.
- **Router privacy:** attachment thumbnails go to Llamacracy, which is the
  author's own infrastructure and keeps no `/v1` content. Nothing goes to a
  third party until Phase 9, which sends *search queries*, never images.
- Plain HTTP inside NetBird is deliberate; see v1's notes on the clipboard and
  `<a download>`.

---

## 11. Metering and admin

Llamacracy's model, used for visibility only.

- **GPU:** `nvidia-smi` sampled every 0.5s while a job holds the GPU (Llamacracy's
  `GpuPowerSampler`). The mean is stored on the job as `gpu_watts_mean`.
- **Energy:** `energy_wh = (gpu_watts_mean + NON_GPU_LOAD_WATTS) ×
  occupancy_seconds / 3600`. Occupancy runs from the moment the worker takes
  the job to its end, model load included; queue waiting is never counted.
  `NON_GPU_LOAD_WATTS` is the configured CPU/system estimate.
- **Cost:** `cost_usd = energy_wh / 1000 × rate`. The rate comes from
  `ELECTRICITY_RATE`, or from `TOU_SCHEDULE` (hour → $/kWh, lifted from
  Llamacracy) when set. `rate_used` and `cost_usd` are **frozen on the row**
  and never recomputed.
- **Everything that actually ran is attributed to its owner,** including failed
  and cancelled jobs. Llamacracy zeroes failures because it bills; AlchemyUI
  tracks, and the energy was really spent.
- **Router tokens** are recorded per turn but not costed here. Llamacracy
  already meters them on its side.
- **Admin page** (`/admin`, live over its own SSE stream):
  - **Live tiles:** GPU watts, temperature, utilisation, VRAM; the running job
    and its owner; queue depth; the resident checkpoint; router status
    (primary reachable, or fallback active).
  - **Per-user table:** jobs, GPU-minutes, kWh and cost over 7 days, 30 days
    or all time.
  - **Recent failures and router fallbacks.**
  - **Users:** disable and enable.
  - Live GPU stats are polled only while an admin page is connected.
- **Users** see their own month in settings: images, kWh and cost.
- The numbers are an estimate of the GPU's share, not a wall-socket reading,
  and the page says so.

---

## 12. Frontend

As simple as possible. It should feel like ChatGPT.

- **Left rail:** "New chat", the user's threads (newest first), "Library" (all
  their images), and an avatar menu with Settings, Admin (admins only) and
  Sign out. On a phone it becomes a drawer.
- **The thread:** user bubbles show their text and attachment thumbnails, each
  numbered *1, 2…* to match the `a1`, `a2` handles.
- **Assistant blocks:**
  - The **plan chip** says in plain words what is happening. Tapping it shows
    the rewritten prompt, model and seed, with Retry.
  - Then **v1's progress card**: queue position, then step count with elapsed
    time and ETA, then live preview frames.
  - Then the images. Each one can be enlarged, downloaded, favourited, or
    added to the next message as an attachment.
- **`needs_input`** shows the question with option buttons. Typing a reply
  answers it too.
- **The composer:** one auto-growing textarea; attach by paperclip, drag and
  drop, or paste (up to 4 images); Enter sends; Stop cancels the running turn.
- **Settings:** likeness photos, and your usage this month.
- **Library:** v1's gallery, minus power tools, with a favourite filter and
  search.
- **Gone from the UI:** mode tabs, sampler/cfg/steps controls, presets, tags,
  the history sheet and the mask editor. Phase 8's region editing may bring
  back a *simplified* region picker for `needs_input`.
- v1 lessons kept: never a bare spinner; optimistic placeholder; the SSE
  reconnects and recovers state; auto-focus only on fine-pointer devices.
  **The boot path is executed in the test suite, not just parsed**, because a
  syntactically valid typo blanked the whole v1 UI once.

---

## 13. ComfyUI API contract

Verified against ComfyUI 0.30.0's source in v1. Unchanged:
- `POST /prompt` with a **client-minted** `prompt_id`, a canonical lowercase
  hyphenated UUID. Always read `node_errors`.
- `POST /upload/image` for inputs.
- WS `?clientId=`: progress comes from `progress_state` (the flat `progress`
  message is gone). Also handle `executing`, `executed`, `execution_error`,
  `execution_interrupted` and `execution_success`, plus binary preview frames.
- `GET /history/<id>` once, on completion; `GET /view` to copy outputs out.
- **Targeted** `POST /interrupt {"prompt_id"}`, and `POST /queue {"delete"}`.

When a detail is uncertain, read `server.py`'s `@routes` and
`script_examples/websockets_api_example.py`. Do not guess.

---

## 14. Phases

### Phases 1–5 — v1 (complete)

1. MVP txt2img
2. Editing: img2img, inpaint, outpaint
3. Quality chain
4. Likeness
5. Platform: model picker, swap-aware queue, batch, previews, favourites and
   tags, history, presets

All of it shipped and was verified live on this laptop. The workflows,
templates, queue, ComfyUI client, face validation and storage carry into v2
unchanged in role.

### Phase 6 — Multi-user platform

The v1 UI keeps working throughout, now scoped per user.

- `users` table and the full Llamacracy `identity.py`; `ADMIN_EMAILS`;
  disabled accounts.
- `user_id` on jobs, images, uploads and refs. Every endpoint is scoped, the
  SSE stream is filtered per user, and the queue snapshot is anonymised.
- Existing rows are claimed by the first admin.
- Queue: strict FIFO across users, with swap reordering only within one
  owner's run.
- Catalogue: checkpoints get folder paths; the new SD1.5 models join, with
  traits; an `sd15-inpaint` family becomes the default for inpaint and
  outpaint. Measure the Hyper models' sampling live.
- Metering: the power sampler, energy and cost frozen on each job,
  `NON_GPU_LOAD_WATTS`, `ELECTRICITY_RATE`, `TOU_SCHEDULE`.
- The admin page and its SSE stream.
- Deploy: an `alchemyui-auth` (oauth2-proxy) unit with its config and env
  example, Dex client instructions, and the new `.env` settings.
- **Acceptance:**
  - Two users, driven by forwarded headers against the real app, each see
    only their own gallery and events.
  - A job from user B queued behind user A's never runs first.
  - Jobs record plausible watts and cost.
  - The admin page shows live GPU stats during a real generation.
  - Inpainting on the RV6 inpaint checkpoint shows no seam.

### Phase 7 — Chat UI and router

- `threads`, `turns`, the turn state machine, handles and scoped resolution.
- `planner/`: `client.py`, `schema.py` (generated from the registry),
  `prompt.py`, `router.py`, `policy.py`.
- The `alchemyui-router` fallback unit, CPU-only.
- The new SPA (§12) replaces v1's composer. Likeness moves into settings.
- `alchemyui router-eval`; choose `ROUTER_PRIMARY_MODEL` by its results.
- **Acceptance:**
  - Examples 1, 2 and 5 from §1 work from the box.
  - Example 3 gets an honest `ask`.
  - "Me as an astronaut" uses the saved likeness.
  - With Llamacracy unreachable, turns still plan on the fallback and say so.
  - The router-eval report exists for both backends.

### Phase 8 — Region-aware editing

- A segmentation custom node plus model, chosen by measuring on 6GB. The
  candidates are CLIPSeg (small and coarse) and GroundingDINO + SAM (accurate
  and heavier). Installing custom nodes is a machine change: ask first.
- `edit_region` with two region sources:
  - **segment by text**, to change something that exists ("the sky");
  - **place by box**, for inserting something that doesn't ("me, on the
    bed"). The box comes from the router's vision model and can be adjusted
    by the user at `needs_input`.
- The inpaint workflows on `sd15-inpaint`, plus FaceID when the subject is a
  person.
- **Acceptance:** example 3 works. Expect "convincingly approximate", not
  Photoshop.

### Phase 9 — Web references

- SearXNG image search. Llamacracy's instance listens on loopback on
  `dex-server`, so the first task is deciding how the laptop reaches it
  (expose it on NetBird, or run its own).
- An **SSRF-safe fetcher**: public addresses only, re-checked on every
  redirect; size and time caps; content sniffed, decoded with Pillow and
  re-encoded. The server never forwards raw bytes.
- A candidate picker in the thread at `needs_input`. References are never
  auto-picked, because web results are too often wrong.
- IPAdapter **Plus** for garment and object references (the model needs
  downloading).
- **Acceptance:** example 4 works end to end.

---

## 15. Non-goals

A node graph editor · per-user limits or credits · billing and invoices · an
OpenAI-compatible API · cloud GPUs · video · model downloading from the app ·
installing custom nodes from the app · an agentic multi-step LLM loop (the plan
schema may grow toward bounded refinement later, not now).

---

## 16. Deliverables (v2)

1. The working multi-user app behind oauth2-proxy, and `DEV_MODE` for local
   development.
2. `workflows/`: templates plus `registry.json`, with a catalogue matching
   §3.
3. `alchemyui validate [--live]` and `alchemyui router-eval`.
4. `deploy/`: the `alchemyui`, `alchemyui-auth` and `alchemyui-router` units,
   an idempotent `install.sh`, and `alchemyui-ctl`.
5. `README.md` and `docs/DECISIONS.md`, kept current phase by phase.
6. `.env.example` covering every new setting.
7. `tests/`: no GPU, no ComfyUI, no LLM needed. Includes multi-user scoping,
   queue fairness, metering arithmetic, plan validation, handle resolution,
   and an executed frontend boot.

---

## 17. Working agreement

- Match Llamacracy's conventions. Its source is on GitHub; the local checkout
  is gone. Where this spec and Llamacracy disagree, raise it rather than
  silently picking.
- Build each phase end to end before starting the next.
- **Test against the real thing:** the real ComfyUI at 127.0.0.1:8188, the real
  Llamacracy, the real local llama-server. Real workflows and real models fail
  in ways mocks do not.
- Nothing machine-specific is committed: models, generated config, secrets,
  API keys and hostnames live in gitignored files that the installer
  scaffolds from committed examples.
- Keep `README.md` and `docs/DECISIONS.md` current as each phase lands.
