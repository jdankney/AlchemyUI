# Design decisions

Why AlchemyUI is shaped the way it is: every non-obvious choice, with the
reasoning, so you can tell which ones to keep when you adapt it.
[ALCHEMYUI_SPEC.md](../ALCHEMYUI_SPEC.md) is the brief these were made against;
where a decision departs from it, that is said explicitly and why.

Llamacracy is the sibling project these conventions come from. "Lifted from
Llamacracy" below means the code is deliberately near-identical, so that
knowing one codebase means knowing both.

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
