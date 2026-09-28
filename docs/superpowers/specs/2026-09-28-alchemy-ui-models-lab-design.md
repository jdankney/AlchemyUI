# Phase 7.5 — Alchemy UI, model choice and the Photoreal Lab

Design agreed 2026-09-28 in a brainstorm with the visual companion. It is three
sub-projects built on one branch, in the order below, so that the switcher's
and the Lab's screens are built in the new style from the start.

## What the author asked for

1. **A UI overhaul**, "super nice and polished". The two complaints were that
   the **images are not the star** and that it **looks generic**.
2. **A model switcher**, picked **per message** from the prompt box.
3. **A way to benchmark settings** so the models lean harder into
   photorealism. Chosen form: an **admin Lab page** that renders a grid of
   settings at low priority, lets you compare same-seed results side by side,
   star the best, and click "Make default".

Direction **C, "Alchemy"**, won from three mockups: midnight blue with a
violet-to-gold glow, frosted-glass panels, and each image glowing with its own
colours. The four-screen mockup (desktop thread with the model menu open, the
image viewer, the Library, and the phone with the menu as a bottom sheet) was
approved as drawn. The mockups are kept in `.superpowers/brainstorm/`, which is
gitignored.

## Assumptions (not stated by the author, decided here)

- **"Models" means the image checkpoints**, not the planner LLM. The menu
  offers the photoreal SDXL and SD1.5 checkpoints: RealVis XL, Realistic
  Vision 6.0, epiCRealism and Realistic Vision Hyper. It leaves out the SD1.5
  base (not photoreal) and the inpainting UNets (the planner uses those only
  for extending).
- **A pick sticks to the thread**: it applies to that message and later ones in
  the same thread until it is changed. A new chat starts with whatever the
  composer last showed, which is remembered per browser.
- **The Lab is admin-only**, and what it tunes is **per-model defaults for
  everyone**: steps, cfg, sampler and scheduler, a positive "look" appended to
  the prompt, and a negative prompt.

## 1. The Alchemy UI

**Tokens.** Midnight background (`#090816`) with two fixed radial glows,
violet top-left and teal bottom-right. Glass surfaces are translucent, with
`backdrop-filter: blur()` and a hairline light edge. The accent is a gradient
from `#a874ff` to `#f0b85a`, used for the wordmark, the primary buttons and
the send button. Violet (`#b48cff`) is the solid accent. Sora is the UI font
and JetBrains Mono the data font. Both are **self-hosted** as latin-subset
woff2 files under the OFL, so the app loads no fonts from Google and works
offline over NetBird.

**Images are the star.**
- **In the thread:** one image is a hero at the thread's full width (up to
  about 760px, and at most 72vh tall). Two sit side by side. Three are one
  large plus two stacked. Four make a 2×2 grid.
- **Actions** (favourite, use in chat, download) appear on hover, and are
  always shown on touch screens.
- **The glow:** every image carries a **palette**, three dominant colours
  computed with Pillow when it is saved (and backfilled for existing images
  at startup). The glow behind an image is layered radial gradients in those
  colours. There is no live blur of the image itself, so it stays smooth on a
  phone.
- **Jobs in flight** render as a frame shaped like the finished image, which
  is known from its width and height. Inside the frame are the live preview,
  the step count, the elapsed time and the ETA. It is never a bare spinner.

**Views.**
- **Rail:** each thread shows a thumbnail of its latest image, grouped
  under Today, Yesterday and Earlier.
- **Library:** justified rows (every image keeps its real shape and each row
  fills the width) grouped by day, with filters for All, Favourites and model,
  plus search.
- **Viewer:** full screen, with the image glowing on dark glass. Arrow keys
  and swipes move through the current list, and Esc closes it. A side panel
  (a bottom sheet on a phone) shows the prompt, the look, the model, the size,
  the seed, how long it took and the settings. Actions: favourite, download,
  copy prompt, use in chat, delete.
- **Settings** and **Admin** use the same glass cards. Admin links to the Lab.

**Motion.** Images fade and rise into place, and the glow blooms in. All of it
is disabled by `prefers-reduced-motion`.

## 2. The model switcher

**Composer.** A pill shows `✦ Auto` or the pinned model's name. It opens a
popover on desktop and a bottom sheet on a phone. Each row shows the model's
cover image (its adopted Lab render, or a gradient monogram before it has
one), its label, a one-line tagline from the registry, whether it is loaded,
and a typical time for one image (from the queue's learned timings, plus the
load time when it is cold).

**API.**
- `GET /api/models` lists the switchable models.
- `POST /api/turns` takes `model` (a checkpoint name, or null for Auto). Any
  name outside the list is a 400.
- The pin is stored on the turn (`turns.model`) and the thread
  (`threads.model`). Answering a question and "Run again" reuse the turn's pin.

**Policy.** The planner still picks words, and code still picks the model. A
pin replaces the trait lookup, with three exceptions where honouring it would
break the result. In each case the model used is explained in a note under the
chip:

| Situation | Model used | Note |
|---|---|---|
| Extend | the inpainting UNet | extending always uses the inpainting model |
| The workflow cannot take the pinned model's family (likeness, enhance: SDXL only) | the policy's own pick | this needs an SDXL model |
| Transform with an SDXL pin and a source under 900px | SD1.5 | the source is too small for SDXL |

The planner's `quality` word is ignored when a model is pinned.

## 3. Profiles and the Photoreal Lab

**Profiles.**
- The `model_profiles` table holds a per-checkpoint override of `steps`,
  `cfg`, `sampler`, `scheduler`, `look` (text appended to the prompt) and
  `negative`. At startup the registry's checkpoint defaults are overlaid with
  it, and again whenever it changes. Everything that reads a checkpoint (the
  policy, the queue's estimates, the models list) sees the tuned values.
- The registry file stays the committed baseline, and "Reset" drops the
  override.
- The Lab can show a profile as registry JSON, ready to commit into
  `workflows/registry.json` once a setting has proved itself.

**How a prompt is composed** (in the policy, and identically in the Lab):
`prompt = plan.prompt + ", " + look` when a look is set, and
`negative = plan.negative + (profile negative, or else the workflow's house
negative)`.

**A Lab run** is defined by:
- models (one to four photoreal checkpoints);
- prompts (from a committed set of eight photoreal test prompts in
  `workflows/lab.json`, plus one custom prompt);
- one to three seeds (drawn once, then shared by every cell, so differences
  come from the settings alone);
- up to two **axes**, each one of `cfg`, `steps`, `sampler` (a
  sampler-and-scheduler pair), `look` or `negative`, with a list of values.

Each cell is one model, prompt, seed and combination of axis values, applied on
top of that model's current profile. A run is capped at 96 cells, and the form
shows the image count and an estimated duration before you start.

**Low priority, honestly.**
- Lab jobs run in a second **lane** of the same queue. A user job is always
  inserted ahead of every lab job, and positions and depth shown to users count
  only user jobs.
- A user job that arrives while a lab job holds the GPU **preempts** it: a
  targeted interrupt, and the cell is requeued behind the user work. So a lab
  run never makes anyone wait longer than an interrupt.
- Lab images are stored as the admin's images, tagged with `lab_run_id`, and
  are excluded from the Library and the chat. Lab GPU time is metered to the
  admin like any other job.

**Reviewing.**
- For each model and prompt, the run page shows a grid: rows are the first
  axis, columns the second, with a tab for each seed. Clicking a cell opens it
  in the viewer, and ←/→ compares its neighbours.
- A star marks a keeper. The **tally** sums stars per model and settings
  combination across all prompts and seeds, and puts the winner on top with
  **Make default**. Making a default writes the profile and makes that cell's
  image the model's cover in the switcher.

## Data model changes

- `threads.model TEXT`, `turns.model TEXT`
- `images.palette TEXT` (a JSON list of three hex colours) and
  `images.lab_run_id TEXT`
- `jobs.lane TEXT NOT NULL DEFAULT 'user'` and `jobs.lab_run_id TEXT`
- `model_profiles (checkpoint PK, settings_json, cover_image_id, updated_by,
  updated_at)`
- `lab_runs (id, created_by, title, spec_json, state, created_at,
  finished_at)`
- `lab_cells (id, run_id, seq, checkpoint, prompt_key, seed, combo_json,
  settings_json, job_id, image_id, state, stars, created_at)`

## Testing

- **Unit tests:** palette extraction; profile validation and overlay; policy
  pins (honoured, and each of the three exceptions); prompt composition; queue
  lanes (ordering, positions, depth, preemption and requeue); Lab expansion,
  the cap, seeds, the tally and adoption.
- **API tests:** `/api/models`, and turns carrying `model`. The Library
  excludes Lab images, and its model filter works. Lab routes are admin-only.
- **Front end:** `app.js`, `admin.js` and `lab.js` boot against a real server
  in the node DOM shim, in every view.
- **Live:** real ComfyUI and Llamacracy. A pinned message is generated with
  each model. A starter Lab run is made on the SD1.5 models, one setting is
  adopted, and the next pinned message is shown using it. Screenshots are taken
  at desktop and phone widths.

## Out of scope

Automatic realism scoring (CLIP or aesthetic models), per-user profiles, the
Lab for inpainting and likeness workflows, and switching the planner LLM.
