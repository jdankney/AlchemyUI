# Design decisions

Why AlchemyUI is shaped the way it is: every non-obvious choice, with the
reasoning, so you can tell which ones to keep when you adapt it.
[ALCHEMYUI_SPEC.md](../ALCHEMYUI_SPEC.md) is the brief these were made against;
where a decision departs from it, that is said explicitly and why.

The first section records the **v2 redesign**. Everything after it was decided
while building v1, Phases 1–5, against the v1 brief
(`git show f3c4a63:ALCHEMYUI_SPEC.md`). Where v2 overturns a v1 decision, the
v1 entry says so rather than being deleted: the reasoning is still the record
of why it was right at the time.

Llamacracy is the sibling project these conventions come from. "Lifted from
Llamacracy" below means the code is deliberately near-identical, so that
knowing one codebase means knowing both.

## v2 — the redesign

Decided 2026-09-25, before any v2 code. The spec's §6–§12 hold the detail;
these are the reasons.

### Shape

- **Multi-user, like Llamacracy, but tracking rather than limiting.** A few
  friends share one GPU. What was wanted from Llamacracy's metering is
  *visibility*: what each person's generations cost in electricity, and the GPU
  live from anywhere. So v2 lifts the power sampler, the cost formula and the
  admin page, and leaves out credits, sessions, caps and invoices. Nothing ever
  blocks a job for cost.
- **One prompt box instead of six modes.** v1 exposed its workflows directly,
  which made the UI a control panel. The workflows were right; making users
  choose among them was not. v2 keeps every one and lets a planner choose.

### The planner

- **The LLM writes a plan, never a graph.** It fills in a small, closed,
  schema-checked vocabulary: `generate`, `transform`, `edit_region`, and so on.
  The registry turns that into a graph and validates it exactly as v1 did. A
  hallucinating model can only choose among options that exist. Letting an LLM
  emit ComfyUI graphs would hand arbitrary node execution to whatever it
  guessed.
- **The LLM picks meaning; code picks numbers.** Plans say
  `strength: "subtle"` and `quality: "fast"`, never `denoise: 0.45` or a
  checkpoint filename. The registry and `policy.py` map words to tuned values.
  Those values were measured on this card over five phases, and a model would
  choose them worse. It also means `policy.py` can prefer the checkpoint that
  is already loaded on `fast`, which a model could not know about.
- **The vocabulary is generated from the registry at boot.** An action is
  offered only when a validated workflow backs it. Until region editing
  exists, "add me to my bed" cannot be misrouted to something that half-works;
  it gets an honest question instead. Adding a capability is still just a
  registry entry.
- **Constrained decoding, then one repair retry.** `response_format:
  json_schema` becomes a llama.cpp grammar, so a well-behaved server can't
  return invalid JSON. The retry, which feeds the validation error back, covers
  a plan that parses but names something that doesn't resolve.
- **Earlier outputs reach the router as text, not pixels.** It sees what each
  previous turn asked for and the prompt behind each image. That is enough to
  resolve "make it darker" or "the second one", at a fraction of the cost of
  re-sending every image to a vision model.

### Where the planner runs

- **Llamacracy first, a local CPU model as fallback.** The laptop's 6GB card
  cannot hold an LLM and an SD checkpoint together; an LLM on it would evict
  the checkpoint on every message and cost a 30s+ reload. Llamacracy already
  serves vision-capable models on another GPU, behind an OpenAI-compatible
  `/v1` that passes `response_format` and images through untouched. So the
  router needs nothing new on that side, just an API key.
- **A fallback that cannot touch the GPU.** Gemma4-E4B on CPU is kept resident
  in about 5GB of 40GB RAM. `-ngl 0` is not sufficient on a CUDA build of
  llama.cpp: large prompt batches and the vision projector can still be
  offloaded. The unit also sets `CUDA_VISIBLE_DEVICES=` so the GPU is simply
  not there.
- **A short timeout on Llamacracy, because router calls queue behind its own
  users.** Its `/v1` shares its FIFO. If someone is mid-way through a long
  answer, waiting would stall a turn that the local model could plan in
  seconds. Llamacracy also doesn't say which model is resident, so a router
  request for a cold model forces a swap there. The configured
  `ROUTER_PRIMARY_MODEL` should therefore be one that is usually loaded.
- **Planning is off the GPU and outside the GPU queue.** It runs concurrently,
  with a small cap per backend. A turn waiting on the user's answer holds no
  place in line, so nobody waits on someone else's reply. A turn takes its
  queue position when its plan becomes final.

### Trust and privacy

- **Plans name images by handle.** `a1` is this turn's attachment, `t4.2` is
  turn 4's second output, `me` is your likeness. Handles resolve only inside
  the requesting user's scope. The LLM's output is treated as untrusted input,
  like a form submission: a plan that names something outside your scope is
  rejected however it came about.
- **The admin page shows metadata, not content.** The person who owns the GPU
  can see who ran what, for how long, and at what cost, but not anyone's
  prompts or images. Saved likenesses, which are photos of friends' faces, are
  what make that line matter.
- **Admins come from `ADMIN_EMAILS`, as in Llamacracy.** Identity is still keyed
  on the OIDC `sub`; the email list is only how an admin is *named* in config,
  and the flag is refreshed on each login.
- **Existing v1 rows are claimed by the first admin to sign in.** v1 had no
  owner column. The alternative, a synthetic "legacy" user, would leave the
  author's own images in an account nobody can log into.

### Queue and metering

- **Strict FIFO across users; swap reordering only within one user's run.** v1
  promoted any job whose checkpoint was loaded. With several people in the
  queue, that reads as someone cutting in line. v2 allows promotion only past
  jobs with the same owner, inside the contiguous same-owner run at the head.
  Promoting a user's later job past someone else's queued job would overtake
  that other person, even if the promoted job belonged to the user at the
  front.
- **Power is sampled by calling `nvidia-smi`, as Llamacracy does**, rather than
  through NVML bindings. It is the same measurement with no new dependency, and
  the laptop's mobile GPU was checked to report `power.draw`.
- **CPU energy is estimated** (`NON_GPU_LOAD_WATTS`), because Intel RAPL is
  root-only here, the standard Platypus side-channel mitigation. The GPU is
  where nearly all of it goes.
- **Failed and cancelled jobs are attributed too.** Llamacracy zeroes them
  because it *bills*, and a failure is not the user's fault. AlchemyUI only
  *tracks*, and the energy was really spent.
- **Cost is frozen on the job row** (`rate_used`, `cost_usd`), lifted from
  Llamacracy, so a rate change never rewrites history.

### Front-end

- **Mode tabs, sampler controls, presets, tags and the history sheet leave the
  UI.** Threads are the history. The columns stay in the schema, harmless,
  in case something comes back.
- **The front-end boot is executed in the test suite, not just parsed.** In v1,
  `const promptPlaceholder = w =` — a missing `>` — was valid syntax, passed
  `node --check`, and blanked the entire UI at module load. A chat UI has more
  boot path to get wrong, not less.


## Phase 6 — as built

What building and running the multi-user platform added to the design above.

- **Verified live, with two users and the real ComfyUI.** Alice ran a job on
  the Hyper checkpoint, then Bob queued SDXL and Alice queued Hyper again.
  They ran **Alice, Bob, Alice**, even though Alice's second job had its model
  already loaded; v1 would have promoted it past Bob. Over the whole run,
  Bob's event stream carried 34 events: none named Alice's jobs or contained
  her prompt text, yet he still saw the queue depth reach 2. The admin page,
  polled 476 times, showed each running job's owner and no prompt text.
- **Your v1 database migrates in place.** 49 jobs, 43 images, 2 references and
  1 preset came through with the integrity check `ok` and no foreign-key
  violations (tested on a copy first). On the first admin request, all 43
  images were claimed by that account.
- **Indexes on new columns are created after the migrations, not in
  `schema.sql`.** `schema.sql` runs first, so an index on a column an old
  database doesn't have yet would abort startup before the `ALTER` that adds
  it.
- **Metering values are stored exact and rounded only for display.**
  Rounding to fixed decimals at storage time broke two ways, both caught by
  tests. A stored energy figure stopped agreeing with the stored occupancy it
  was supposed to be derived from. And small values became zero: an API
  reporting kWh to five places returned `0.0` for a short job. SQLite `REAL`
  is a double, so there was nothing to save by rounding.
- **The admin page polls, as Llamacracy's does, and is its own page.**
  Polling keeps `nvidia-smi` from running when nobody is looking, without a
  second event stream. A separate `/admin` page means Phase 7's SPA rewrite
  never has to carry it.
- **A laptop on battery looks exactly like a broken app.** Unplugged, the
  driver caps the 1660 Ti at **10 of its 80 W** (P8, 300 MHz, "SW Power Cap:
  Active"). SDXL then ran at **54.5 s/step against 5.25 s/step** plugged in,
  and a six-step Hyper image took 75 s. The metering was honest about it
  (12.7 W measured), but nothing said *why*. The admin page now reads the
  enforced power limit and mains state and says so in words. The same run
  also showed why ETAs can't be trusted on battery: the per-step EWMA learns
  from whatever it observes. The acceptance runs used a scratch database, so
  the real one's timings were not polluted.
- **The dedicated inpainting UNet works through the existing templates.**
  ComfyUI builds a 9-channel model's extra input from the sampler's
  `latent_image` and `denoise_mask` when no `concat_latent_image` is given
  (`comfy/model_base.py: concat_cond`), which is exactly what
  `VAEEncodeForInpaint` supplies. So paint-over and extend needed only a
  catalogue change. Live: a lighthouse repainted as a lit stone building,
  with the stars running unbroken through where the mask's edge was.
- **Measured on mains, at 512² / 1024²** (the registry's timing seeds now use
  these figures):

  | Checkpoint | Steps | Wall time | s/step | GPU draw | Energy |
  |---|---|---|---|---|---|
  | Realistic Vision Hyper | 6 | 7.0 s warm, 8.8 s cold | 1.05 | 67–75 W | 0.2 Wh |
  | Realistic Vision 6.0 | 25 | 16.3 s | 0.57 | 75 W | 0.45 Wh |
  | RealVis XL | 25 | 132 s | 5.06 | 74 W | 3.6 Wh |

  SDXL's 5.06 s/step against v1's 5.25 confirms the battery diagnosis above.
  Hyper steps cost about twice as much because `dpmpp_sde` evaluates the model
  twice per step.
- **Hyper keeps its defaults (6 steps, cfg 1.5, `dpmpp_sde`, karras) and does
  not become the default "fast" model.** Its output is clean, but in two of
  two images it stamped fake watermark text into a corner. At cfg 1.5 the
  negative prompt, which already says "watermark", has almost no effect, so
  nothing in the prompt can suppress it. RV 6.0 is only nine seconds slower,
  doesn't do it, and is the crisper of the two on the same seed. So Phase 7's
  policy should map `fast` to RV 6.0 and keep Hyper for drafts.
- **The front-end boot is executed in the suite.** `tests/js/boot.mjs` runs
  each page's real script, in node, against a real server. With v1's
  blank-page typo put back into `app.js`, it reports `ReferenceError: w is not
  defined` and 0 rendered nodes, where `node --check` passes. On its first
  run it also caught `admin.js` leaving a failed fetch unhandled at boot.
- **Tests read the developer's `.env`.** `Settings` loads the repo's `.env`,
  so a test that depends on who the dev identity is has to pin `DEV_EMAIL`
  itself. The front-end test does; it failed until it did, because this
  machine's `DEV_EMAIL` is not `dev@localhost`.
- **The installer ships unexecuted.** It enables linger, downloads
  oauth2-proxy and writes systemd units. It is syntax-checked, and the control
  script it installs was exercised, but registering AlchemyUI in Dex is a
  change on dex-server: the installer prints the exact `staticClients`
  entry rather than making it.

## Architecture

- **FastAPI + one SQLite connection (WAL) behind one asyncio lock**, lifted
  from Llamacracy. Writes are tiny (a job row, an image row) and the queue is
  serial anyway. Timestamps are epoch floats throughout.
- **Vanilla JS front-end, no build step, no framework — and no CDN at all.**
  The spec allows cdnjs classic scripts with a fallback, which Llamacracy needs
  for markdown, sanitising, highlighting and maths. AlchemyUI renders none of
  those, so the whole blocked-CDN fallback problem is solved by not having the
  dependency. One `app.js`, one hand-written `styles.css`.
- **Declarative registry → validated at boot.** The same
  inventory-drives-everything idea as Llamacracy's `bench/inventory.json`,
  applied to workflows. Adding a capability is a JSON entry plus an exported
  graph; no backend changes.
- **The app is the only thing that talks to ComfyUI.** Port 8188 has no auth
  and `/prompt` accepts arbitrary graphs — arbitrary node execution and
  filesystem access. Even finished images are copied out and served by the app,
  never proxied from `/view`.

## Divergences from the spec

These are the places the spec and the installed reality disagreed. Each was
checked against ComfyUI 0.30.0's source rather than guessed.

- **`progress_state`, not `progress`.** Spec §6 lists a flat
  `progress` → `{value, max}` message. ComfyUI 0.30 **no longer sends it**:
  `WebUIProgressHandler` (`comfy_execution/progress.py`) emits only
  `progress_state` → `{prompt_id, nodes: {<id>: {value, max, state, …}}}`.
  Building to the spec verbatim would have shipped a progress bar that never
  moves. We read the registry's declared `progress_node` (the KSampler) out of
  that map, and keep a legacy `progress` branch for older servers.
- **We mint the `prompt_id`.** `/prompt` accepts a client-supplied id if it is
  a canonical lowercase hyphenated UUID (`server.py: validate_job_id`). Choosing
  it ourselves removes the race where WebSocket events for a prompt arrive
  before the POST response does: the relay can key on it from the moment of
  submit. The spec assumes the server mints it.
- **Targeted interrupt.** `POST /interrupt {"prompt_id": …}` only aborts if
  that prompt is the running one. The spec's bare `/interrupt` would kill
  whatever happens to be executing — including work someone started from
  ComfyUI's own UI.
- **One long-lived SSE stream, not Llamacracy's per-request one.** Llamacracy
  streams tokens on the `POST /api/chat` response because the tokens *are* the
  response. An image job takes minutes, outlives any request, and the user is
  expected to navigate away. So `POST /api/generate` returns `{job_id}`
  immediately and everything else arrives on one `GET /api/events`
  `EventSource`, which brings its own reconnect backoff. Anything missed while
  disconnected is recovered from `/api/jobs` and `/api/images`.
- **`est.seconds_per_step` is 5.25, not the spec's implied 2.4.** Spec §3 says
  SDXL 1024² × 25 steps takes ~60s. Measured on this box (GTX 1660 Ti Mobile,
  6GB, `--fp8_e4m3fn-unet`) it is ~131s, twice that. The seed now reflects what
  was actually observed; the EWMA corrects it per machine anyway.
  `load_s` stays at the spec's conservative 35s: the 3.8s load observed here
  was a page-cache-warm read of a just-downloaded file, not a cold one.

## The queue

- **Strict FIFO, one generation at a time.** One GPU, so there is nothing to
  gain from concurrency and a great deal to lose (VRAM).
- The app keeps its **own** queue and submits one prompt at a time. ComfyUI's
  internal queue is not the source of truth: we need real positions, a cancel
  that aborts, and pending work that survives a restart.
- **`position` counts jobs ahead of you, not place in line.** The UI says
  "2 ahead of you" literally, so the number has to be true. The first job into
  an empty queue is 0 — there is nobody in front of it.
- **Load and sampling are learned separately.** A checkpoint swap costs 30s+
  regardless of step count; sampling is linear in steps. The first sampler step
  is the boundary between them, so that is where the split is measured. Same
  EWMA shape as Llamacracy's `model_load_stats`.
- **Cancelling a running job really aborts it**: `/interrupt` with the
  prompt_id, then we wait for ComfyUI's `execution_interrupted` before calling
  it cancelled rather than assuming.
- **A restart fails what was running and keeps what was queued.** Anything left
  `running` is unrecoverable — either we restarted or ComfyUI did — and becomes
  `failed` with a message saying so. Reconciliation also interrupts those
  orphaned prompt_ids: if only the app restarted, ComfyUI is still grinding on
  a result nobody will ever see.
- **Shutdown does not finalise an in-flight job.** When the worker task is
  cancelled the row is deliberately left `running` and nothing is written;
  startup reconciliation is the only thing that knows the generation did not
  survive. Claiming success there would record an image that never existed.
- **The resident checkpoint is tracked, not queried.** ComfyUI has no
  equivalent of llama-swap's `/running`, so it is inferred from the last job to
  complete and persisted in `runtime_state`. It is only ever presented as a
  hint ("cold start ~35s"), never as fact.
- **Checkpoint-swap-aware ordering is a seam, not an implementation.** Spec §7
  asks for it. Phase 1 has exactly one checkpoint, so reordering would be
  untested dead code; `_pick_next()` is the single place it will live. Same
  treatment Llamacracy gives `SMALL_MODEL_FAST_LANE`.

## The workflow registry

- **API format, not UI format.** The regular Save in ComfyUI writes node
  positions, link arrays and `widgets_values`, which `/prompt` rejects at
  validation. That is the single most likely mistake when adding a workflow, so
  the loader detects it specifically and says "re-export with Save (API
  Format)" rather than failing as a generic parse error.
- **Every binding is validated at boot, and the failure is loud.** Node IDs
  change whenever a graph is rebuilt in the editor. A drifted binding that only
  surfaces mid-generation costs a minute of GPU time to discover and reads like
  a model problem. The same check ships as `alchemyui validate` so it can run
  in an installer, and `--live` additionally asks ComfyUI whether the node
  classes and checkpoints exist.
- **Node IDs never cross the API boundary.** `/api/workflows` exposes controls,
  defaults, limits and choices; the graph, the template name and the bindings
  stay server-side. The browser cannot express a workflow the registry has not
  sanctioned.
- **Parameters are resolved and clamped server-side, then stored whole.** What
  the UI sends is a suggestion; `resolve()` fills defaults, clamps to `limits`
  and rejects sampler names outside `choices`. The complete resolved set is
  written to both the job and every image row — that is what makes "reuse these
  settings", "vary this seed" and a reproducibility trail possible, and it is
  much cheaper now than retrofitted.
- **Seeds are capped at 2^63-1, not ComfyUI's 2^64-1.** SQLite's INTEGER is
  signed 64-bit, so an unsigned seed cannot be stored on the image row — this
  broke roughly half of all unseeded generations before it was caught. A seed
  we cannot record is a seed the gallery cannot reproduce, so the cap is on the
  generator rather than the column.

## Storage

- **Outputs are copied out of ComfyUI into `OUTPUT_DIR`,** one directory per
  job. ComfyUI's layout shifts with `filename_prefix`, date tokens and custom
  nodes, and an image in the gallery should not vanish because someone tidied
  up. The gallery is served from app-owned files.
- **PNG dimensions are parsed from the IHDR chunk** rather than adding Pillow.
  It is twenty lines and Phase 1 needs nothing else from an imaging library.
  Phase 2's mask work and Phase 4's face normalisation will want the real one.
- A missing file on disk returns **410, not 404**: the row exists and the bytes
  do not, which is a different problem from a bad id and deserves to look like
  one.

## Errors

- **`node_errors` is logged verbatim and kept on the row.** It names the exact
  node and field, and is the single most useful artifact when a template
  drifts. The UI gets a one-line version; the full payload stays behind a
  disclosure.
- **A failure is never retried silently.** On a GTX 16-series card, black or
  NaN output is an fp16 precision problem, not an app bug (spec §3). Retrying
  would hide the one signal that distinguishes them. (In practice this box
  generates clean SDXL images with `--fp8_e4m3fn-unet`.)
- **A job that finishes without producing an image is a failure,** not an empty
  success — with the `/history` payload attached, because that is where the
  reason will be.

## Editing (Phase 2)

- **Three separate templates, not one parameterised graph**, as the spec
  requires. img2img runs `VAEEncode`; inpaint and outpaint run
  `VAEEncodeForInpaint`. Collapsing them would mean a graph with branches the
  registry cannot express and a validator that cannot check them.
- **White means replace, end to end.** That is what the person brushes, what
  the exported PNG encodes, what `LoadImageMask` reads out of the **red**
  channel, and what `VAEEncodeForInpaint` blanks. The `alpha` channel was
  avoided deliberately: `LoadImage` returns `1.0 - alpha` as its mask, so
  routing through alpha inverts the polarity and repaints everything *except*
  what was brushed — a failure that looks like a model problem, not a wiring
  one. There is a test asserting the red channel specifically.
- **Outpaint is the inpaint path with a generated mask.**
  `ImagePadForOutpaint` returns `(IMAGE, MASK)` with the new border already
  marked, so the same encoder does both jobs and there is one less thing to
  keep correct.
- **Uploads are normalised on arrival**, not at generation time: converted to
  RGB PNG, scaled so the longest edge fits `UPLOAD_MAX_EDGE` (1536 — a 6GB
  card OOMs on a phone photo), and snapped to a multiple of 8. The snap
  matters more than it looks: the VAE downscales by 8 and `VAEEncodeForInpaint`
  silently *crops* a non-multiple, which would shift the image relative to the
  mask painted over it.
- **EXIF orientation is applied, not carried.** The mask is painted against
  what the browser displayed; if the pixels still need rotating afterwards,
  the mask lines up with nothing.
- **The mask is scoped to its upload** (`POST /api/uploads/{id}/mask`) rather
  than being a standalone file, so it can be checked against the exact
  dimensions of the image it was drawn over.
- **The mask is uploaded before the job is submitted.** A mask the server
  rejects should stop the submit, not fail a generation that already holds
  the GPU.
- **Masks are binarised, not anti-aliased.** Half-lit edge pixels become a
  partial noise mask and a smeared seam. `grow_mask_by` is what feathers the
  join, deliberately and in one place.
- **`grow_mask` defaults to 32, against ComfyUI's own 6.** Measured on real
  output at ~1150px: at 6 there is a clearly visible ring around the mask,
  because RealVisXL is not an inpainting checkpoint and refills the blanked
  region with a slightly different colour balance. 32 reduces it markedly.
  It cannot remove it entirely — that would need an inpainting checkpoint —
  so the slider goes to 64 and the trade is explicit: a better seam costs more
  of what you kept.
- **Outpaint pads are capped at 512 a side and step in 8s.** Every padded
  pixel is generated, so the cost is real and a 6GB card finds the ceiling
  quickly. The UI draws the resulting canvas to scale, because four numbers do
  not tell you that you just asked for a panorama.
- **The denoise slider is labelled, not numbered.** 0.62 means nothing to most
  people; "Strong — keeps the rough shapes" does. The range is the spec's
  0.3–0.9: below that nothing visibly changes, above it the source stops
  mattering and you may as well have generated from scratch.
- **The time estimate accounts for denoise.** KSampler runs `steps × denoise`
  of them, so quoting the full step count would overstate every img2img run.

## The quality chain (Phase 3)

- **Two ways in, because the spec's single toggle asks for a blind
  commitment.** The chain roughly triples the wait — on this GPU that is two
  minutes becoming six — and a toggle on Generate means spending six minutes
  on an image you have not seen. So there is also an **Enhance** mode that
  takes an image you already have. You look at the base result, then decide it
  is worth the wait. The author chose both; the toggle is there as specced,
  and the Enhance mode is the one that also works on inpaint and outpaint
  output.
- **The HQ toggle swaps in a different registry entry**, it does not modify a
  graph. `txt2img-sdxl` declares `hq_variant: txt2img-sdxl-hq`, and that twin
  is a separate template validated at boot like any other. Editing a graph at
  submit time would throw away the guarantee the whole registry design exists
  to provide.
- **`scale_by` is recomputed per job to hold a VRAM ceiling.** The chain is
  `4x upscale -> scale back`, so the output edge is `4 x source x scale_by`.
  The spec's 0.38 lands at ~1556px from a 1024px base, which fits; from a
  1344px base it would be 2043px and the sampler runs out of 6GB. Rather than
  fail, the app lowers `scale_by` so the result fits `HQ_MAX_EDGE` and records
  what it actually used, so the params stored on the image stay truthful.
  The chain degrades to "less upscaling" instead of "crashed".
- **`force_inpaint` is False, against the node's own default of True.**
  Measured, not guessed. Impact Pack computes an upscale factor for the
  detected face; when that is <= 1 -- the face is already bigger than
  `guide_size` -- `force_inpaint: True` falls back to sampling the *entire*
  crop anyway. On a portrait that means re-rendering the whole 1552px image
  for no resolution gain: it cost 5 minutes of a 12-minute run. `False` makes
  it skip that case, which is the whole point of a detail pass. A small face
  in a large image still gets one.
- **`refine_steps` is 14, not 25.** The hi-res pass runs at denoise ~0.35, so
  it only traverses a third of the schedule; 25 steps across that stretch is
  far finer than the output justifies, and it is the most expensive stage in
  the chain. Halving it made no visible difference to the result.
- **Measured cost, after both:** enhancing a 1024px image took 749s before and
  **248s** after. The HQ toggle (base + chain) is **372s against a 133s base
  — 2.8x**, which is the "roughly triples" the spec promises. Before the
  tuning it was 5.6x, and the UI would have been lying about the wait.
- **Tiled VAE encode and decode (512/64)** are what let a 6GB card touch these
  resolutions at all. The sampler still sees the whole latent; only the VAE
  round-trip is tiled.
- **Every sampler in the chain takes the job's seed.** A multi-stage graph has
  three of them, and "reuse these settings" would be a lie if reproducing a
  result needed three numbers instead of one.
- **FaceDetailer runs without a SAM model.** `sam_model_opt` is optional, and
  no SAM weights are installed. Its `sam_*` inputs are still *required* by the
  node, so they carry its own defaults and mean nothing. With no face in
  frame, FaceDetailer passes the image through untouched — which is why the
  chain is safe to run on a landscape.
- **A generated image can be fed back in** (`POST /api/images/{id}/reuse`).
  The gallery lives in `OUTPUT_DIR` and ComfyUI's `LoadImage` can only see its
  own `input/`, so an image has to make that trip before it can be enhanced or
  edited. It goes through the same normalisation as a fresh upload: one code
  path, one set of guarantees about size.

## Likeness (Phase 4)

The spec names two failure modes it has already hit in practice. Both are now
prevented on upload rather than handled at generation time, because both kill
a job *after* the queue wait and the model load.

- **Every reference is face-checked on its own, the moment it is added.**
  InsightFace raises "No face detected." if any one image in a batch fails, and
  takes the whole job with it. Checking each on arrival means the bad one is
  refused by name, with something actionable ("No face found in meadow.png"),
  and nothing reaches a generation unless every reference is already known good.
- **Every reference leaves normalisation at an identical 768×768 square.**
  `ImageBatch` rescales and centre-crops later images to match the *first*,
  which can slice a face out of frame. If they all arrive the same size,
  ImageBatch has nothing left to do. There is a test asserting that references
  from wildly different source aspect ratios all come out identical.
- **InsightFace runs inside AlchemyUI, on the CPU.** The spec says to normalise
  "with PIL", but PIL cannot find a face, so it can neither centre one nor say
  which reference is bad — and both are things the spec explicitly asks for.
  The models are reused from ComfyUI's `models/insightface` rather than
  downloaded again, and `onnxruntime` (not `-gpu`) keeps the GPU entirely for
  the sampler. It is a second copy of the detection stack, and worth it: the
  check is immediate, it names the file, and it works when ComfyUI is down.
  The app has insightface 2.0 against ComfyUI's 1.0.1, but both read the same
  `buffalo_l` weights, so a disagreement between them is unlikely.
- **The crop stays square and is clamped to the image, never padded or
  stretched.** A face near an edge gives a slightly off-centre crop, which is
  much better than a distorted one. The square side is the face box times a
  margin, because FaceID wants head and shoulders rather than a tight box.
- **The largest face wins when several are found, and the count is reported.**
  A bystander in the background should not cost someone their upload; the UI
  marks the thumbnail so the choice is visible.
- **The whole reference chain lives in the template; a job prunes it.** All
  four slots and all three `ImageBatch` nodes are in the file, so the boot
  validator checks every one of them — including that the chain really is a
  chain, which is what pruning assumes. A job keeps as many as it was given,
  repoints the consumer, and drops the tail. Building the chain dynamically
  would have meant graph surgery the validator could not see.
- **The personal LoRA node is bypassed, not set to zero strength.** Bypassing
  reroutes each output back to the input it would have passed through, exactly
  as ComfyUI's own bypass does. Zero strength would still load the file, and
  the only LoRAs on disk are FaceID's own — which
  `IPAdapterUnifiedLoaderFaceID` already applies, so loading one again would
  have it fighting its own weights. They are filtered out of the picker for
  the same reason.
- **A personal LoRA lowers the default face weight to 0.45.** The spec's
  0.4–0.5: a LoRA and FaceID both encode identity and pull against each other
  at full strength. Only the *default* moves — an explicit weight is respected.
- **Both FaceID weights are exposed.** `weight` and `weight_faceidv2` trade off
  differently, which is why the spec asks for two sliders rather than one
  number.
- **Measured:** one reference 172s, two 178s, against a 133s plain generation.
  SDXL FaceID *does* fit on the 6GB card — the open question from the planning
  stage — so Phase 4 stays on RealVisXL like everything else and no SD1.5
  fallback was needed. Faces come out slightly over-smooth at the default
  weights, which is characteristic of FaceID PlusV2; the sliders are there for
  it.

## The platform layer (Phase 5)

- **Checkpoints are first-class registry entries, not a field on a workflow.**
  Resolution and sampling belong to the model: SD1.5 falls apart much above
  512–768 a side and SDXL is trained at ~1024, so a shared "aspect" table
  would be wrong for one of them. A checkpoint carries its own aspects,
  defaults and timing seeds; picking a model switches all of it together.
- **One template serves both families.** `CheckpointLoaderSimple` does not care
  which weights it loaded, so txt2img, img2img, inpaint and outpaint each
  declare `families: [sdxl, sd15]` and the same validated graph runs both.
  Only the quality chain and likeness are SDXL-only — their tile sizes and
  FaceID presets were measured for SDXL, and SD1.5 variants would need their
  own numbers rather than an assumption.
- **A workflow's declared cost is a ratio, not an absolute.** `est` is quoted
  against the workflow's own default checkpoint, so what the workflow
  contributes is "2.9x a plain generation", which holds on either family.
  Taking its SDXL seconds literally made SD1.5 look exactly as slow as SDXL —
  caught by a test that asserted the cheaper model estimates cheaper.
- **Timings are learned per workflow *and* per checkpoint.** SD1.5 measured
  ~1.2s/step against SDXL's ~5.25s; one average across both would be wrong for
  each of them, and the ETA is the main thing the UI promises.

## Checkpoint-swap-aware queueing

The seam left open in Phase 1, now that there is more than one checkpoint to
reorder around.

> **v2:** kept, but restricted to one user's own jobs; see *Queue and
> metering* above. What follows is the single-user reasoning.

- **A swap costs 30s+** — a quarter of an SDXL generation and more than a whole
  SD1.5 one. So within a bounded look-ahead, a job whose checkpoint is already
  resident is promoted ahead of one that would force a load.
- **Two things keep it honest.** The window is capped
  (`QUEUE_REORDER_WINDOW`), so a job can only be overtaken by jobs near the
  front. And every job passed over counts a skip; at `QUEUE_MAX_SKIPS` it runs
  next regardless of cost. Strict FIFO is the fallback, not the exception.
- **With one checkpoint in play it does nothing at all**, which is exactly what
  Phase 1 promised when the seam was left empty. There is a test asserting
  that.
- **Measured:** submitting SD1.5, SDXL, SD1.5 ran them SD1.5, SD1.5, SDXL —
  one model load instead of two.

## Live previews

- **Frames come over HTTP; the event stream only says a newer one exists.** A
  frame is 20–60KB and arrives once per sampling step; pushing that down the
  SSE stream would swamp a channel that also carries queue state. The browser
  fetches at whatever rate it can keep up with, with one request in flight per
  job, so a slow connection drops frames rather than queueing them.
- **Frames live in memory only** and are released the moment the job ends —
  they are worthless once the next arrives, and the real image supersedes them.
- **Previews require ComfyUI to be started with `--preview-method auto`**; it
  defaults to none. Their absence is normal and never looks like a fault.
- Frames carry no prompt_id, but only one job ever holds the GPU, so attaching
  them to the active job is unambiguous.

## Gallery, history and presets

- **Batch generation is n separate jobs, not one graph with `batch_size: n`.**
  They queue, report progress and cancel individually, and a 6GB card will not
  hold a batched SDXL latent. An explicit seed still increments across the
  batch — otherwise "4 images" is four copies of one image.
- **Prompt history is read back out of the images table**, not kept separately.
  Storing the full parameter set on every image was what made "reuse these
  settings" and the reproducibility trail possible; it doubles as the history
  and the search index for free, with nothing to keep in sync.
- **Search and filters run server-side.** The gallery only holds what has been
  fetched, so filtering in the browser would quietly hide older matches.
- **Tags are normalised on the way in** — lowercased, de-duplicated, trimmed,
  capped — and matched with comma boundaries, so filtering on `port` does not
  match `portrait`.
- **Saving a preset over an existing name updates it** rather than erroring.
  Renaming to reuse a name is a chore, and there is one user. (v2 takes
  presets, tags and the history sheet out of the UI; see above.)

## Not built, and why

- **ControlNet.** No ControlNet models are on disk and no preprocessor nodes
  are installed, so there is nothing to build against. The spec hedged it with
  "if VRAM allows" and the honest answer is that it needs models first.
- **A LoRA library with thumbnails.** The only LoRAs present are FaceID's own,
  which are deliberately filtered out of the picker (see Phase 4). The picker
  exists and works; it has nothing to show until a personal LoRA is added.

## Multi-stage progress

- **A job can report progress from more than one node, and the bar has to
  reflect the whole run.** The HQ graph samples three times — base, hi-res,
  faces. Following one node would fill the bar a third of the way through a
  six-minute job and then sit there, which is exactly the "bare spinner"
  failure the spec's UX rules exist to prevent.
- Each workflow declares its `stages` with a `weight` — a rough share of the
  wall clock. Overall progress is the weighted sum. Rough is fine: the weight
  only has to stop the bar from lying. The Faces stage is weighted low
  precisely because it usually skips (see `force_inpaint` above); it only
  under-runs the bar when the pass genuinely happens.
- **Reaching stage N means every earlier stage is complete**, whether or not
  it ever reported. Some nodes never emit progress, and without that rule a
  silent stage would stall the bar at the previous stage's total.
- The bar never goes backwards: a new stage starting from step 1 would
  otherwise drop it.
- The card names the stage — "Refining detail · 7/25", not "Step 7 of 25" —
  because three passes each counting to 25 otherwise look like one stuck bar.
- **The ETA switches from modelled to measured** once a job is a little way
  in: extrapolating from its own elapsed time against overall progress beats a
  per-step average that knows nothing about which stage is running.

## Deployment

- **Asked and answered (spec §3): ComfyUI stays on the laptop.** Pointing
  AlchemyUI at the 1080 Ti would put SDXL in contention with Llamacracy's
  resident model, and 11GB does not hold a 7B GGUF and SDXL at once. No
  co-residency control has been built, deliberately. If that ever changes, the
  fix is to call Llamacracy's llama-swap unload endpoint before a generation
  and let the idle TTL do the rest — extending Llamacracy's "one model
  resident" rule across both apps rather than inventing a second mechanism.

## The mask editor

The hardest front-end piece in the project, per the spec. Plain Canvas API, no
library.

- **Strokes are stored as vectors in the source image's own pixel
  coordinates**, not as bitmap snapshots. Undo is then exact and free, the
  display canvas can be laid out at any size, and the export is always at the
  image's true resolution. A snapshot stack would cost ~7MB per step at
  1536×1152 and cap undo depth at a handful.
- **One routine draws both the on-screen overlay and the exported mask**; only
  the colours differ. That is what guarantees what you painted is what gets
  replaced — there is no second implementation to drift out of sync.
- The eraser is `destination-out`, and the export flattens onto black
  afterwards so the red channel is never ambiguous where a stroke was erased.
- `touch-action: none` on the canvas, or the browser scrolls the page instead
  of letting you draw on a touchscreen.
- Pointer events (not mouse events) with `setPointerCapture`, so a stroke that
  leaves the canvas still ends cleanly, and pen/touch work for free.
- A tap with no movement still leaves a dot — the stroke is closed against
  itself rather than discarded.

## Identity

> **v2 overturns this section.** There is a users table, identity is lifted
> whole from Llamacracy, and friends sign in through Dex. The refusal rule
> below is kept exactly.

- **There is no auth code, and no users table.** Single user, bound to
  127.0.0.1, `DEV_MODE=1`. Nothing in the schema is keyed per user.
- The one rule kept from Llamacracy is **the refusal**: with `DEV_MODE` unset
  the app requires oauth2-proxy's forwarded headers and returns 503 without
  them, so it can never accidentally end up serving open on a network.
- Sharing with friends means **registering a second OAuth client in
  Llamacracy's existing Dex** and running a second oauth2-proxy — not writing
  auth here. Users would key on the OIDC `sub`, never the email; `Principal`
  already carries it so the day something needs it, it is the right key.
- **Plain HTTP is deliberate** (WireGuard is the encryption layer), which is
  why there is no "copy image" button: the async Clipboard API needs a secure
  context. `<a download>` does not, so downloads work fine.

## Front-end

- **Never a bare spinner.** Waits here are minute-scale, so a job card always
  says where it actually is: queue position, then step count, then elapsed and
  a learned ETA. An indeterminate bar appears only in the genuinely unknown
  window between submit and the first sampler step.
- **A 0/1 progress frame is not a step.** ComfyUI reports `value: 0, max: 1`
  for any node that is merely *running* — the checkpoint loader, the text
  encoders, and the sampler before it starts. Treating those as progress
  flashed "step 0 of 1" before the real bar appeared, so a max of 1 on a job
  asking for more steps than that is ignored.
- **Optimistic placeholder on submit**, replaced by the real job id when the
  POST returns; if the POST fails, the card is withdrawn and the prompt handed
  back rather than lost.
- **One input and one button by default.** Negative prompt is one disclosure
  in; sampler internals are nested one deeper. Aspect ratio stays visible
  because it is a real choice people make every time — shown as a box in the
  actual proportions, which reads faster than the numbers.
- **The elapsed clock ticks on its own timer.** Nothing pushes an event between
  sampler steps, so without it the card would look frozen for five seconds at a
  time.
- Composer auto-focus only fires on devices with a fine pointer, so a phone
  never gets its keyboard summoned by a re-render (a Llamacracy lesson).

## Testing

- **The suite needs no GPU and no ComfyUI.** `FakeComfy` emits the same
  WebSocket sequence a real 0.30 server does, including `progress_state`, so
  the relay is exercised exactly as it runs in production.
- **Registry validation is pure JSON**, which is what keeps it runnable
  anywhere — in CI, in an installer, before a model has been downloaded.
- **The SSE endpoint is tested over a real socket.** httpx's ASGI transport
  buffers a response to completion before returning it, and an event stream
  never completes, so `TestClient` deadlocks on `/api/events`. The event stream
  is the spine of this app; it gets a real uvicorn on an ephemeral port rather
  than no coverage.
- Tests assert against the **shipped** `workflows/registry.json` and template,
  not a fixture copy, so a drifted binding fails the suite and not just the app.

## Operations

- **One systemd user unit**, not Llamacracy's four: there is no auth edge and
  no model server to supervise. ComfyUI keeps its own lifetime and its own
  launch flags — pretending otherwise would make `install.sh` the wrong place
  to debug a GPU problem.
- uvicorn runs with `--timeout-graceful-shutdown 5`: an open SSE connection
  never closes on its own, and without the bound the unit hangs on SIGTERM
  until systemd kills it and reports a failure.
- `install.sh` validates the registry **before** starting anything, so a
  drifted workflow is caught at deploy time rather than on the first prompt.
- The CLI is `alchemyui-ctl`, because `alchemyui` is the app's own command
  (`alchemyui validate`). Slightly awkward, and better than two things with one
  name.
