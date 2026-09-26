# Phase 7 — Chat UI and Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace v1's six-mode composer with one ChatGPT-style prompt box: every message is a turn in a thread, a small LLM turns it into a validated plan, and code turns the plan into queued ComfyUI jobs.

**Architecture:** A `planner/` package produces a closed-vocabulary `Plan` (never a graph) from Llamacracy's `/v1` (primary) or a local CPU llama-server (fallback), with schema-constrained decoding whose enums are generated from the registry *and from the handles in the requesting user's own scope*. `policy.py` compiles a plan into job specs (workflow, checkpoint, tuned numbers). `threads.py` owns threads, turns and the turn state machine and submits through the existing `QueueManager`, whose job-finished hook moves turns along. The SPA is rewritten around threads.

**Tech Stack:** FastAPI, Python 3.14, uv, SQLite (WAL, one connection), httpx, Pillow, vanilla JS (no build, no CDN), SSE, systemd user units, llama-server 0.4.1.

**Spec:** `ALCHEMYUI_SPEC.md` (v2) — §6 turn pipeline, §7 router, §9 data model, §10 privacy, §12 frontend, §14 Phase 7.

## Global Constraints

- The LLM never writes a graph; it writes a plan in a closed vocabulary. Code validates it.
- LLM output is untrusted input: every image handle resolves inside the requesting user's own scope, or the plan is rejected.
- Never expose port 8188. No CDN, no JS framework, no build step.
- Nothing machine-specific is committed: model names, URLs, keys and model paths live in the gitignored `.env`; `.env.example` documents them.
- `MAX_BATCH` default 4. `ROUTER_CONTEXT_TURNS` default 6. Thumbnails ≤768px.
- Planning concurrency: 2 for the primary, 1 for the CPU fallback.
- Fallback llama-server: `-ngl 0` **and** `CUDA_VISIBLE_DEVICES=`; its own unit `alchemyui-router`.
- Thinking off (`chat_template_kwargs: {"enable_thinking": false}`).
- Every turn records `plan_source` (`primary` | `fallback` | `default`), router latency and token counts.
- Tests need no GPU, no ComfyUI, no LLM. The SPA boot path is *executed* in the suite.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Gate every commit on pytest's real exit code, never `pytest | tail`.

## Findings that shape this plan (verified 2026-09-25)

- Text-only E4B (`gemma4-e4b-uncensored`) on Llamacracy returns schema-valid plans in ~4s warm (6s cold). Flat schemas with `enum`, `maxLength` and integer `minimum`/`maximum` are enforced.
- **Llamacracy currently rejects image input on every model**, E4B and 26B alike: llama-server there runs without `--mmproj` (`500 image input is not supported`). The router therefore treats "no vision" as its own error kind: a turn with attachments falls through to the local fallback, which has the mmproj. When gpu-server gains its mmproj, nothing changes here.
- The Gemma 4 chat template honours `enable_thinking` (default false).

## Deviations from the spec's letter (to record in DECISIONS and the spec)

1. **A `select` action** ("use the second one") joins the vocabulary. It moves the thread's focus to an image and queues nothing. Without it, example 5's middle step has no honest route.
2. **Stored turn states** are `planning | needs_input | queued | done | failed | cancelled`. The spec diagram's `submitted` and `ready` are instants, not resting states, and `running` lives on the jobs. The chip still shows "running" from the job.
3. **Handle enums are generated per call.** The spec says the schema is generated from the registry at boot; the *actions* are, and on top of that `source` and `faces` are enums of the handles that exist in this user's scope for this turn, so the grammar cannot emit a handle that fails to resolve. Server-side resolution still checks every handle (hard rule 3).
4. **Trait `draft`.** DECISIONS (Phase 6) found Hyper watermarks and said "map `fast` to RV 6.0 and keep Hyper for drafts". The Hyper checkpoints move from `fast` to `draft`; RV6 and epiCRealism gain `fast`.

---

## File Structure

**Create**
- `alchemyui/jobs.py` — build and submit jobs from resolved params; import a gallery image as a ComfyUI input. Shared by `/api/generate` and the turn engine.
- `alchemyui/planner/__init__.py` — package marker, re-exports.
- `alchemyui/planner/schema.py` — `Vocabulary` (from the registry), `plan_schema(vocab, sources, faces)`, `Plan`, `validate_plan()`, `PlanInvalid`.
- `alchemyui/planner/policy.py` — `compile_plan()` → `Compiled` | `NeedsInput`; checkpoint choice; strength/direction numbers; the chip text.
- `alchemyui/planner/prompt.py` — system prompt, context lines, thumbnails, message assembly, repair message.
- `alchemyui/planner/client.py` — `LLMClient` (OpenAI-compatible chat with `response_format`), `LLMError` kinds, reachability.
- `alchemyui/planner/router.py` — `Router.route()`: primary → repair → fallback → repair → default; `default_plan()`.
- `alchemyui/threads.py` — `ChatEngine`: threads, turns, the state machine, handle scope and resolution, job hook, restart reconcile.
- `alchemyui/chat.py` — `/api/threads`, `/api/turns` routes.
- `alchemyui/evals.py` — `router-eval` runner and report.
- `evals/router/cases.json`, `evals/router/images/*.jpg` — the eval fixture set (AI-generated images).
- `deploy/systemd/alchemyui-router.service` — CPU-only llama-server.
- Tests: `tests/test_planner_schema.py`, `tests/test_policy.py`, `tests/test_router.py`, `tests/test_threads.py`, `tests/test_chat_api.py`, `tests/test_deploy.py`, `tests/test_evals.py`.

**Modify**
- `alchemyui/schema.sql`, `alchemyui/db.py` — `threads`, `turns`; `jobs.turn_id`, `images.turn_id`, `refs.is_likeness`.
- `alchemyui/config.py` — router settings; `max_batch` 4.
- `alchemyui/queue.py` — `Job.turn_id`; `add_listener()`; public `emit()`; `turn_id` on job and image rows.
- `alchemyui/app.py` — `/api/generate` via `jobs.py`; engine lifecycle; likeness cap and filter on `/api/references`; include `chat.router`.
- `alchemyui/admin.py` — router status on `/live`; `/fallbacks`.
- `alchemyui/registry.py`, `workflows/registry.json` — `draft` trait.
- `alchemyui/cli.py` — `router-eval`.
- `alchemyui/static/index.html`, `assets/app.js`, `assets/styles.css` — the new SPA. `assets/admin.js` — router tile and fallbacks.
- `deploy/install.sh`, `deploy/alchemyui-cli.sh`, `.env.example`.
- `tests/test_catalogue.py`, `tests/test_frontend.py`, `tests/conftest.py`.
- `README.md`, `docs/DECISIONS.md`, `ALCHEMYUI_SPEC.md`.

---

### Task 1: Data model and settings

**Files:**
- Modify: `alchemyui/schema.sql`, `alchemyui/db.py`, `alchemyui/config.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: tables `threads(id, user_id, title, created_at, updated_at, deleted)`, `turns(...)` per spec §9 plus `answers_json`, `reply`; columns `jobs.turn_id`, `images.turn_id`, `refs.is_likeness` (existing rows become likenesses). Settings `router_primary_url/key/model/concurrency`, `router_fallback_url/model/concurrency`, `router_timeout_s`, `router_fallback_timeout_s`, `router_context_turns`, `router_thumb_edge`, `max_batch=4`.

- [ ] **Step 1: Failing tests** in `tests/test_db.py`:

```python
def test_threads_and_turns_exist(tmp_db_path):
    db = Database(tmp_db_path)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(turns)")}
    assert {"thread_id", "user_id", "seq", "text", "attachments_json", "state", "plan_json",
            "plan_source", "plan_error", "question_json", "answers_json", "reply", "router_ms",
            "prompt_tokens", "completion_tokens", "created_at", "planned_at",
            "finished_at"} <= cols
    assert {"id", "user_id", "title", "created_at", "updated_at", "deleted"} <= {
        r[1] for r in db._conn.execute("PRAGMA table_info(threads)")}
    db.close()


def test_phase7_columns_migrate_onto_a_phase6_db(tmp_db_path):
    """A Phase 6 database gains turn_id and is_likeness, and its existing
    reference faces become the owner's saved likeness."""
    import sqlite3
    c = sqlite3.connect(tmp_db_path)
    c.executescript("""
        CREATE TABLE refs (id TEXT PRIMARY KEY, user_id INTEGER, path TEXT NOT NULL,
          comfy_name TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', width INTEGER NOT NULL,
          height INTEGER NOT NULL, faces_found INTEGER NOT NULL DEFAULT 1, face_score REAL,
          face_px INTEGER, created_at REAL NOT NULL);
        INSERT INTO refs VALUES ('r1', 1, 'p', 'c', '', 768, 768, 1, 0.9, 200, 1);
    """)
    c.close()
    db = Database(tmp_db_path)
    assert db._conn.execute("SELECT is_likeness FROM refs WHERE id='r1'").fetchone()[0] == 1
    for t in ("jobs", "images"):
        assert "turn_id" in {r[1] for r in db._conn.execute(f"PRAGMA table_info({t})")}
    db.close()
```

- [ ] **Step 2: Run** `uv run pytest tests/test_db.py -q` — expect both FAIL.

- [ ] **Step 3: Implement.** Append to `schema.sql` (before `schema_meta`):

```sql
-- v2 / Phase 7: conversations. A thread is a list of turns; a turn is one
-- message and everything it caused. Threads are the history now.
CREATE TABLE IF NOT EXISTS threads (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    deleted     INTEGER NOT NULL DEFAULT 0   -- hidden from the rail; its images stay in the library
);
CREATE INDEX IF NOT EXISTS ix_threads_user ON threads(user_id, deleted, updated_at DESC);

CREATE TABLE IF NOT EXISTS turns (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT NOT NULL REFERENCES threads(id),
    user_id           INTEGER NOT NULL REFERENCES users(id),
    seq               INTEGER NOT NULL,        -- per-thread ordinal: the "4" in t4.2
    text              TEXT NOT NULL DEFAULT '',
    attachments_json  TEXT NOT NULL DEFAULT '[]',   -- upload ids, in a1..a4 order
    state             TEXT NOT NULL,           -- planning|needs_input|queued|done|failed|cancelled
    plan_json         TEXT,                    -- {"plan": ..., "compiled": ..., "chip": ...}
    plan_source       TEXT,                    -- primary|fallback|default
    plan_error        TEXT,                    -- why a backend was skipped or a plan rejected
    question_json     TEXT,                    -- {"question": str, "options": [{"label", "plan"}]}
    answers_json      TEXT NOT NULL DEFAULT '[]',   -- [{"question", "answer"}], replayed to the router
    reply             TEXT,                    -- words shown under the chip (select, ask, failures)
    router_ms         INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at        REAL NOT NULL,
    planned_at        REAL,
    finished_at       REAL,
    UNIQUE (thread_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_turns_state ON turns(state);
```

Add `turn_id TEXT` to `jobs` and `images`, and `is_likeness INTEGER NOT NULL DEFAULT 1` to `refs` in `schema.sql` (fresh DBs). In `db.py` append to `_MIGRATIONS`:

```python
    # v2 / Phase 7: turns own jobs and images; references become likenesses.
    # DEFAULT 1 is deliberate: every v1 reference face was a likeness photo.
    "ALTER TABLE jobs ADD COLUMN turn_id TEXT",
    "ALTER TABLE images ADD COLUMN turn_id TEXT",
    "ALTER TABLE refs ADD COLUMN is_likeness INTEGER NOT NULL DEFAULT 1",
```

and to `_INDEXES`: `"CREATE INDEX IF NOT EXISTS ix_jobs_turn ON jobs(turn_id)"`, `"CREATE INDEX IF NOT EXISTS ix_images_turn ON images(turn_id, created_at)"`.

In `config.py`, change `max_batch: int = 8` → `4` (spec §6), and add a router block:

```python
    # --- router (v2 / Phase 7) ----------------------------------------------
    # Primary: Llamacracy's /v1 with the `alchemyui` account's key. Empty URL
    # or model disables it. Fallback: the local CPU-only llama-server unit.
    router_primary_url: str = ""
    router_primary_key: str = ""
    router_primary_model: str = ""
    router_primary_concurrency: int = 2
    router_fallback_url: str = "http://127.0.0.1:8081/v1"
    router_fallback_model: str = "gemma4-e4b"
    router_fallback_concurrency: int = 1
    # Llamacracy queues us like any user and may have to wake the model.
    router_timeout_s: float = 45.0
    # CPU prompt processing of an image is slow; this is a fallback, not a race.
    router_fallback_timeout_s: float = 240.0
    router_context_turns: int = 6
    router_thumb_edge: int = 768
```

- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Commit** `Phase 7: threads, turns, turn ownership on jobs and images, likeness flag`.

---

### Task 2: Job submission extracted; queue hooks

**Files:**
- Create: `alchemyui/jobs.py`
- Modify: `alchemyui/app.py` (`generate`, `_record_upload`), `alchemyui/queue.py`
- Test: `tests/test_queue.py`, existing `tests/test_api.py` stays green

**Interfaces:**
- Produces:
  - `jobs.record_upload(db, comfy, settings, data: bytes, user_id: int) -> dict` (moved from app.py, same return shape).
  - `jobs.image_as_upload(db, comfy, settings, image_row, user_id) -> sqlite3.Row` — the upload row.
  - `jobs.submit_jobs(qm, db, settings, workflow, raw: dict, checkpoint: CheckpointInfo|None, user_id: int, *, count=1, explicit_seed=False, upload=None, turn_id=None) -> list[Job]` — resolves, applies the hq edge ceiling, varies seeds, submits, records input paths. Raises `JobRejected(msg)`.
  - `QueueManager.add_listener(cb: Callable[[Job], Awaitable[None]])` — awaited after a job is finalised (and after a queued job is cancelled).
  - `QueueManager.emit(event, owner)` — public alias of `_emit`.
  - `Job.turn_id: str | None`, included in `Job.public()` and persisted; image rows get `turn_id`; the `image` SSE event carries `turn_id`.

- [ ] **Step 1: Failing test** in `tests/test_queue.py`:

```python
async def test_listener_hears_every_finished_job_and_turn_id_persists(tmp_db_path, tmp_output_dir, monkeypatch):
    qm, comfy, db = await _started_queue(tmp_db_path, tmp_output_dir, monkeypatch)
    heard = []
    async def on_done(job):
        heard.append((job.id, job.state.value, job.turn_id))
    qm.add_listener(on_done)
    w = qm.reg.require("txt2img-sdxl")
    job = await qm.submit(w, w.resolve({"prompt": "x"}), user_id=None, turn_id="turn-1")
    await asyncio.wait_for(job._done.wait(), 5)
    await asyncio.sleep(0.05)
    assert heard == [(job.id, "completed", "turn-1")]
    row = await db.fetch_one("SELECT turn_id FROM jobs WHERE id = ?", (job.id,))
    assert row["turn_id"] == "turn-1"
    img = await db.fetch_one("SELECT turn_id FROM images WHERE job_id = ?", (job.id,))
    assert img["turn_id"] == "turn-1"
    await qm.stop()
```

(`_started_queue` — reuse the helper already in `tests/test_queue.py`; if its name differs, use that file's existing fixture that builds a `QueueManager` over `FakeComfy`.)

- [ ] **Step 2: Run** — FAIL (`submit()` has no `turn_id`).

- [ ] **Step 3: Implement.**
  - `queue.py`: `Job.turn_id: str | None = None`; `submit(..., user_id=None, turn_id=None)`; `"turn_id"` in `public()`; `_persist_job` writes `turn_id` (insert column list + value; not in the update set — it never changes); `_collect` inserts `turn_id` on images and adds `"turn_id"` to the row dict; `_reconcile` restores `turn_id=r["turn_id"]`.
  - Listeners: `self._listeners: list = []`; `add_listener(cb)`; `async def _notify(job)` awaits each, logging and swallowing exceptions (a listener bug must never kill the worker). Call `_notify(job)` at the end of the worker's `finally` (not when shutting down) and at the end of the queued-cancel branch of `cancel()`.
  - `emit = _emit` as a public method: `async def emit(self, event, owner=EVERYONE): await self._emit(event, owner)`.
  - `jobs.py`: move `_record_upload` here as `record_upload`; move the body of `generate()` from "`params = workflow.resolve(raw, chosen)`" through the job loop into `submit_jobs`, raising `JobRejected` where `generate` raised 400. `image_as_upload` = the body of `reuse_image` returning the upload row (`SELECT * FROM uploads WHERE id = ?` after `record_upload`).
  - `app.py`: `generate` keeps request validation (workflow, upload, references, checkpoint family) and calls `submit_jobs(... count=req.batch, explicit_seed=req.seed is not None, upload=upload)`, mapping `JobRejected` → 400. `create_upload` and `reuse_image` call `jobs.record_upload` / `jobs.image_as_upload`.

- [ ] **Step 4: Run** `uv run pytest -q` — all pass, including every v1 API test.
- [ ] **Step 5: Commit** `Phase 7: job submission shared by the API and the turn engine; queue listeners`.

---

### Task 3: Vocabulary, plan schema and validation

**Files:**
- Create: `alchemyui/planner/__init__.py`, `alchemyui/planner/schema.py`
- Modify: `alchemyui/registry.py` (`KNOWN_TRAITS` += `draft`), `workflows/registry.json` (traits), `tests/test_catalogue.py`
- Test: `tests/test_planner_schema.py`

**Interfaces:**
- Produces:

```python
ACTIONS_KIND = {"generate": "txt2img", "transform": "img2img", "enhance": "enhance",
                "likeness": "likeness", "extend": "outpaint"}
NO_GPU = ("select", "ask")
STRENGTHS = ("subtle", "medium", "strong")
QUALITIES = ("fast", "best")
DIRECTIONS = ("", "wider", "taller", "left", "right", "up", "down", "all")

@dataclass(frozen=True)
class Vocabulary:
    actions: tuple[str, ...]            # registry-backed actions + select + ask
    workflows: dict[str, str]           # action -> workflow key
    aspects: tuple[str, ...]
    max_count: int
    @classmethod
    def from_registry(cls, reg: Registry, max_count: int) -> "Vocabulary": ...

@dataclass(frozen=True)
class Plan:
    action: str; source: str; faces: str; prompt: str; negative: str; aspect: str
    count: int; strength: str; quality: str; direction: str; question: str
    options: tuple[str, ...]
    def as_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "Plan": ...

class PlanInvalid(Exception): ...

def plan_schema(vocab: Vocabulary, sources: list[str], faces: list[str]) -> dict: ...
def validate_plan(raw: str | dict, vocab: Vocabulary, sources: list[str], faces: list[str]) -> Plan: ...
```

- [ ] **Step 1: Failing tests** `tests/test_planner_schema.py`:

```python
from pathlib import Path
import json
import pytest
from alchemyui.planner.schema import Plan, PlanInvalid, Vocabulary, plan_schema, validate_plan
from alchemyui.registry import load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
V = Vocabulary.from_registry(REG, max_count=4)


def _raw(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse at night",
                negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                direction="", question="", options=[])
    base.update(kw)
    return base


def test_vocabulary_comes_from_the_registry():
    assert V.actions == ("generate", "transform", "enhance", "likeness", "extend", "select", "ask")
    assert V.workflows["generate"] == "txt2img-sdxl"      # never the -hq variant
    assert V.workflows["extend"] == "outpaint-sdxl"
    assert "edit_region" not in V.actions and "with_reference" not in V.actions
    assert V.aspects == ("1:1", "3:4", "4:3", "9:16", "16:9")


def test_a_missing_workflow_removes_its_action():
    from alchemyui.registry import Registry
    reg = Registry({k: w for k, w in REG._workflows.items() if w.kind != "likeness"},
                   REG._checkpoints)
    assert "likeness" not in Vocabulary.from_registry(reg, 4).actions


def test_schema_enumerates_only_handles_in_scope():
    s = plan_schema(V, sources=["a1", "t2.1"], faces=["me"])
    props = s["properties"]
    assert props["source"]["enum"] == ["", "a1", "t2.1"]
    assert props["faces"]["enum"] == ["", "me"]
    assert props["action"]["enum"] == list(V.actions)
    assert props["count"] == {"type": "integer", "minimum": 1, "maximum": 4}
    assert set(s["required"]) == set(props) and s["additionalProperties"] is False


def test_valid_plan_round_trips():
    p = validate_plan(json.dumps(_raw()), V, [], [])
    assert p.action == "generate" and Plan.from_dict(p.as_dict()) == p


@pytest.mark.parametrize("raw, why", [
    ("not json", "not valid JSON"),
    (_raw(action="edit_region"), "action"),
    (_raw(action="transform", source=""), "needs a source image"),
    (_raw(action="transform", source="t9.9"), "t9.9"),
    (_raw(action="generate", source="a1"), "generate makes a new image"),
    (_raw(action="likeness", faces=""), "whose face"),
    (_raw(action="likeness", faces="t1.1"), "t1.1"),
    (_raw(action="generate", prompt="  "), "prompt"),
    (_raw(action="extend", source="a1", direction=""), "direction"),
    (_raw(action="ask", question=""), "question"),
    (_raw(aspect="7:5"), "aspect"),
])
def test_invalid_plans_say_why(raw, why):
    with pytest.raises(PlanInvalid, match=why):
        validate_plan(raw if isinstance(raw, str) else json.dumps(raw), V, ["a1"], ["me", "a1"])


def test_count_is_clamped_not_rejected():
    assert validate_plan(_raw(count=9), V, [], []).count == 4
    assert validate_plan(_raw(count=0), V, [], []).count == 1
```

Update `tests/test_catalogue.py`: allowed traits `{"fast", "quality", "photoreal", "inpaint", "draft"}`, and add:

```python
def test_hyper_is_a_draft_model_and_rv6_is_the_fast_one():
    """Phase 6 measured Hyper watermarking; `fast` means RV 6.0 now."""
    for c in REG.all_checkpoints():
        if "Hyper" in c.name:
            assert "draft" in c.traits and "fast" not in c.traits, c.name
    fast = [c.name for c in REG.all_checkpoints() if "fast" in c.traits]
    assert fast[0] == "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
```

- [ ] **Step 2: Run** — FAIL (no module).

- [ ] **Step 3: Implement** `schema.py`:

```python
"""The router's closed vocabulary (spec section 7).

Generated, never hand-listed: an action exists only if a validated workflow
backs it, so Phase 8's `edit_region` cannot be offered before it works. On
top of that, `source` and `faces` are enums of the handles that exist in the
requesting user's scope *for this turn* -- the grammar cannot even emit a
handle that would fail to resolve. Resolution still checks (hard rule 3).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ..registry import Registry

ACTIONS_KIND = {"generate": "txt2img", "transform": "img2img", "enhance": "enhance",
                "likeness": "likeness", "extend": "outpaint"}
NO_GPU = ("select", "ask")
NEEDS_SOURCE = ("transform", "enhance", "extend", "select")
NEEDS_PROMPT = ("generate", "transform", "likeness", "extend")
STRENGTHS = ("subtle", "medium", "strong")
QUALITIES = ("fast", "best")
DIRECTIONS = ("", "wider", "taller", "left", "right", "up", "down", "all")
MAX_PROMPT = 600
MAX_NEGATIVE = 240


class PlanInvalid(Exception):
    """A plan that parsed but does not hold together. The message is fed back
    to the model verbatim on the repair retry, so it says what to change."""


@dataclass(frozen=True)
class Vocabulary:
    actions: tuple[str, ...]
    workflows: dict[str, str]
    aspects: tuple[str, ...]
    max_count: int

    @classmethod
    def from_registry(cls, reg: Registry, max_count: int) -> Vocabulary:
        workflows: dict[str, str] = {}
        for action, kind in ACTIONS_KIND.items():
            # The -hq variants are reached by quality, never as an action.
            found = [w for w in reg.of_kind(kind) if not w.key.endswith("-hq")]
            if not found and kind == "txt2img":
                found = [w for w in reg.all() if w.kind == "txt2img"]
            if found:
                workflows[action] = found[0].key
        actions = tuple(a for a in ACTIONS_KIND if a in workflows) + NO_GPU
        gen = reg.get(workflows.get("generate", ""))
        ck = reg.checkpoint(gen.checkpoint) if gen else None
        aspects = tuple((ck.aspects if ck and ck.aspects else (gen.aspects if gen else {})))
        return cls(actions, workflows, aspects or ("1:1",), max(1, max_count))


@dataclass(frozen=True)
class Plan:
    action: str
    source: str
    faces: str
    prompt: str
    negative: str
    aspect: str
    count: int
    strength: str
    quality: str
    direction: str
    question: str
    options: tuple[str, ...]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["options"] = list(self.options)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        return cls(**{**d, "options": tuple(d.get("options") or ())})


def plan_schema(vocab: Vocabulary, sources: list[str], faces: list[str]) -> dict:
    props = {
        "action": {"enum": list(vocab.actions)},
        "source": {"enum": ["", *sources]},
        "faces": {"enum": ["", *faces]},
        "prompt": {"type": "string", "maxLength": MAX_PROMPT},
        "negative": {"type": "string", "maxLength": MAX_NEGATIVE},
        "aspect": {"enum": list(vocab.aspects)},
        "count": {"type": "integer", "minimum": 1, "maximum": vocab.max_count},
        "strength": {"enum": list(STRENGTHS)},
        "quality": {"enum": list(QUALITIES)},
        "direction": {"enum": list(DIRECTIONS)},
        "question": {"type": "string", "maxLength": 240},
        "options": {"type": "array", "maxItems": 4,
                    "items": {"type": "string", "maxLength": 60}},
    }
    # Every field required, none optional: a flat, fully-required object is
    # the grammar llama.cpp enforces most reliably, and small models fill a
    # neutral "" far better than they decide whether a key should exist.
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


def validate_plan(raw: str | dict, vocab: Vocabulary, sources: list[str],
                  faces: list[str]) -> Plan:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanInvalid(f"the reply was not valid JSON ({e.msg})")
    if not isinstance(raw, dict):
        raise PlanInvalid("the reply was not a JSON object")

    def s(key: str, limit: int = 1000) -> str:
        return str(raw.get(key) or "").strip()[:limit]

    action = s("action")
    if action not in vocab.actions:
        raise PlanInvalid(f"action {action!r} is not one of {', '.join(vocab.actions)}")
    source, face = s("source"), s("faces")
    if source and source not in sources:
        raise PlanInvalid(f"source {source!r} is not an image in this conversation")
    if face and face not in faces:
        raise PlanInvalid(f"faces {face!r} is not a face this user has provided")
    if action in NEEDS_SOURCE and not source:
        raise PlanInvalid(f"{action} needs a source image; set source to one of "
                          f"{', '.join(sources) or '(none -- use ask instead)'}")
    if action == "generate" and source:
        raise PlanInvalid("generate makes a new image from words only; to change an "
                          "existing image use transform, enhance or extend with that source")
    if action == "likeness" and not face:
        raise PlanInvalid("likeness needs to know whose face: set faces to "
                          f"{' or '.join(faces) or 'me'}")
    prompt = s("prompt", MAX_PROMPT)
    if action in NEEDS_PROMPT and not prompt:
        raise PlanInvalid(f"{action} needs a prompt describing the image to make")
    direction = s("direction")
    if direction not in DIRECTIONS:
        raise PlanInvalid(f"direction {direction!r} is not one of {DIRECTIONS[1:]}")
    if action == "extend" and not direction:
        raise PlanInvalid("extend needs a direction: " + ", ".join(DIRECTIONS[1:]))
    question = s("question", 240)
    if action == "ask" and not question:
        raise PlanInvalid("ask needs a question for the user")
    aspect = s("aspect") or vocab.aspects[0]
    if aspect not in vocab.aspects:
        raise PlanInvalid(f"aspect {aspect!r} is not one of {', '.join(vocab.aspects)}")
    strength = s("strength") if s("strength") in STRENGTHS else "medium"
    quality = s("quality") if s("quality") in QUALITIES else "fast"
    try:
        count = int(raw.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    options = tuple(str(o).strip()[:60] for o in (raw.get("options") or []) if str(o).strip())[:4]
    return Plan(action, source, face, prompt, s("negative", MAX_NEGATIVE), aspect,
                max(1, min(vocab.max_count, count)), strength, quality, direction,
                question, options)
```

`planner/__init__.py`: docstring only ("The router: text + attachments in, a validated plan out.").

`registry.py`: `KNOWN_TRAITS = frozenset({"fast", "quality", "photoreal", "inpaint", "draft"})` with the comment extended ("`draft`: few-step Hyper models -- quick, but they watermark; never chosen for `fast`").

`workflows/registry.json` traits: RealVisXL `["quality","photoreal"]`; Hyper `["draft","photoreal"]`; RV6 `["fast","photoreal"]`; epiCRealism `["fast","photoreal"]`; v1-5 `[]`; RV6 inpainting `["inpaint","photoreal"]`; Hyper inpainting `["inpaint","draft"]`. Order unchanged except RV6 must precede epiCRealism (it already does).

- [ ] **Step 4: Run** `uv run pytest -q` — pass.
- [ ] **Step 5: Commit** `Phase 7: the router's vocabulary and plan schema, generated from the registry`.

---

### Task 4: Policy — plan to jobs

**Files:**
- Create: `alchemyui/planner/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `Plan`, `Vocabulary`, `Registry`.
- Produces:

```python
DENOISE = {"subtle": 0.35, "medium": 0.55, "strong": 0.75}

@dataclass(frozen=True)
class PolicyContext:
    resident: str | None                    # checkpoint ComfyUI is believed to hold
    source_size: tuple[int, int] | None     # (w, h) of the resolved source image
    has_likeness: bool

@dataclass(frozen=True)
class Compiled:
    action: str
    workflow: str | None                    # None for select
    checkpoint: str | None
    raw: dict                               # handed to submit_jobs / workflow.resolve
    count: int
    chip: str                               # "Generating · Realistic Vision · 3:4"
    def as_dict(self) -> dict: ...

class NeedsInput(Exception):
    def __init__(self, question: str, options: list[dict] | None = None): ...
    # options: [{"label": str, "plan": dict | None}]

def pick_checkpoint(reg, workflow, trait, resident, family=None) -> CheckpointInfo: ...
def compile_plan(plan: Plan, vocab: Vocabulary, reg: Registry, ctx: PolicyContext) -> Compiled: ...
```

- [ ] **Step 1: Failing tests** `tests/test_policy.py`:

```python
from pathlib import Path
import pytest
from alchemyui.planner.policy import Compiled, NeedsInput, PolicyContext, compile_plan
from alchemyui.planner.schema import Plan, Vocabulary
from alchemyui.registry import load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
V = Vocabulary.from_registry(REG, 4)
RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
EPIC = "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"
RV6_INPAINT = "SD1.5-Inpaint/realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16.safetensors"


def plan(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="3:4", count=1, strength="medium", quality="fast", direction="",
                question="", options=())
    base.update(kw)
    return Plan(**base)


def ctx(resident=None, size=None, likeness=True):
    return PolicyContext(resident, size, likeness)


def test_fast_generation_is_rv6_and_best_is_sdxl():
    assert compile_plan(plan(), V, REG, ctx()).checkpoint == RV6
    assert compile_plan(plan(quality="best"), V, REG, ctx()).checkpoint == XL


def test_fast_prefers_whatever_fast_model_is_already_loaded():
    assert compile_plan(plan(), V, REG, ctx(resident=EPIC)).checkpoint == EPIC
    # a resident model without the trait is not a reason to use it
    assert compile_plan(plan(), V, REG, ctx(resident=XL)).checkpoint == RV6


def test_generate_carries_meaning_through_as_numbers():
    c = compile_plan(plan(count=3, negative="text"), V, REG, ctx())
    assert c.workflow == "txt2img-sdxl" and c.count == 3
    assert c.raw["prompt"] == "a lighthouse" and c.raw["aspect"] == "3:4"
    assert c.raw["negative"].startswith("text, ")           # the model's words, then the house list
    assert c.chip == "Generating 3 · Realistic Vision · 3:4"


@pytest.mark.parametrize("word, denoise", [("subtle", 0.35), ("medium", 0.55), ("strong", 0.75)])
def test_strength_words_become_denoise(word, denoise):
    c = compile_plan(plan(action="transform", source="a1", strength=word), V, REG,
                     ctx(size=(1024, 1024)))
    assert c.workflow == "img2img-sdxl" and c.raw["denoise"] == denoise


def test_a_small_source_stays_on_sd15_even_for_best():
    """SDXL is trained at 1024; handing it a 512px image makes mush."""
    c = compile_plan(plan(action="transform", source="t1.1", quality="best"), V, REG,
                     ctx(size=(512, 768)))
    assert REG.checkpoint(c.checkpoint).family == "sd15"


def test_extend_wider_pads_both_sides_on_the_inpainting_unet():
    c = compile_plan(plan(action="extend", source="a1", direction="wider"), V, REG,
                     ctx(size=(512, 512)))
    assert c.checkpoint == RV6_INPAINT
    assert (c.raw["pad_left"], c.raw["pad_right"], c.raw["pad_top"], c.raw["pad_bottom"]) == (128, 128, 0, 0)


def test_enhance_runs_on_the_one_family_it_supports():
    c = compile_plan(plan(action="enhance", source="a1"), V, REG, ctx(size=(1024, 1024)))
    assert c.workflow == "enhance-sdxl" and c.checkpoint == XL


def test_likeness_without_a_saved_likeness_asks():
    with pytest.raises(NeedsInput, match="saved a likeness"):
        compile_plan(plan(action="likeness", faces="me"), V, REG, ctx(likeness=False))


def test_likeness_from_an_attachment_does_not_need_a_saved_one():
    c = compile_plan(plan(action="likeness", faces="a1"), V, REG, ctx(likeness=False))
    assert c.workflow == "likeness-sdxl"


def test_ask_and_select():
    with pytest.raises(NeedsInput) as e:
        compile_plan(plan(action="ask", question="Which one?", options=("1", "2")), V, REG, ctx())
    assert e.value.question == "Which one?"
    assert [o["label"] for o in e.value.options] == ["1", "2"]
    c = compile_plan(plan(action="select", source="t2.2"), V, REG, ctx())
    assert c.workflow is None and c.count == 0 and "t2.2" not in c.chip
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** `policy.py`:

```python
"""Meaning in, numbers out (spec section 7.3).

The model picks words -- subtle, fast, wider -- and this file turns them into
denoise values, checkpoints and pixel counts. The numbers live here, where
they were measured, rather than in a small model's guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..registry import CheckpointInfo, Registry, WorkflowInfo
from .schema import Plan, Vocabulary

DENOISE = {"subtle": 0.35, "medium": 0.55, "strong": 0.75}
# SDXL is trained at 1024 and turns a small source to mush; below this edge an
# image stays on SD1.5 whatever quality was asked for.
SDXL_MIN_SOURCE_EDGE = 900
VERB = {"generate": "Generating", "transform": "Transforming", "enhance": "Enhancing",
        "likeness": "Generating you", "extend": "Extending"}


@dataclass(frozen=True)
class PolicyContext:
    resident: str | None
    source_size: tuple[int, int] | None
    has_likeness: bool


@dataclass(frozen=True)
class Compiled:
    action: str
    workflow: str | None
    checkpoint: str | None
    raw: dict
    count: int
    chip: str

    def as_dict(self) -> dict:
        return asdict(self)


class NeedsInput(Exception):
    def __init__(self, question: str, options: list[dict] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


def pick_checkpoint(reg: Registry, workflow: WorkflowInfo, trait: str, resident: str | None,
                    family: str | None = None) -> CheckpointInfo:
    """First checkpoint with the trait that this workflow can run, in catalogue
    order -- unless one of them is already loaded, which saves a 30s swap."""
    usable = [c for c in reg.all_checkpoints() if c.family in workflow.families]
    if family:
        usable = [c for c in usable if c.family == family] or usable
    cands = [c for c in usable if trait in c.traits]
    if not cands:
        return reg.checkpoint(workflow.checkpoint) or usable[0]
    for c in cands:
        if c.name == resident:
            return c
    return cands[0]


def _pads(direction: str, size: tuple[int, int] | None) -> dict:
    w, h = size or (512, 512)

    def px(edge: int, share: float) -> int:
        return max(64, min(512, int(edge * share) // 8 * 8))

    pads = dict(pad_left=0, pad_right=0, pad_top=0, pad_bottom=0)
    if direction == "wider":
        pads.update(pad_left=px(w, .25), pad_right=px(w, .25))
    elif direction == "taller":
        pads.update(pad_top=px(h, .25), pad_bottom=px(h, .25))
    elif direction == "all":
        pads = dict(pad_left=px(w, .2), pad_right=px(w, .2),
                    pad_top=px(h, .2), pad_bottom=px(h, .2))
    else:
        side = {"left": "pad_left", "right": "pad_right", "up": "pad_top", "down": "pad_bottom"}
        pads[side[direction]] = px(w if direction in ("left", "right") else h, .5)
    return pads


def compile_plan(plan: Plan, vocab: Vocabulary, reg: Registry, ctx: PolicyContext) -> Compiled:
    if plan.action == "ask":
        raise NeedsInput(plan.question, [{"label": o, "plan": None} for o in plan.options])
    if plan.action == "select":
        return Compiled("select", None, None, {}, 0, "Working from the image you picked")
    if plan.action == "likeness" and plan.faces == "me" and not ctx.has_likeness:
        raise NeedsInput("You haven't saved a likeness yet. Attach a clear photo of your "
                         "face, or add some in Settings, then ask again.")

    workflow = reg.require(vocab.workflows[plan.action])
    family = None
    if plan.action == "extend":
        trait = "inpaint"
    elif plan.quality == "best":
        trait = "quality"
    else:
        trait = "fast"
    if (plan.action in ("transform", "extend") and ctx.source_size
            and max(ctx.source_size) < SDXL_MIN_SOURCE_EDGE):
        family = "sd15-inpaint" if plan.action == "extend" else "sd15"
        if trait == "quality":
            trait = "fast"
    ck = pick_checkpoint(reg, workflow, trait, ctx.resident, family)

    house = str(workflow.defaults.get("negative") or "")
    negative = ", ".join(x for x in (plan.negative, house) if x)
    raw: dict = {"prompt": plan.prompt, "negative": negative}
    if "width" in workflow.params:
        raw["aspect"] = plan.aspect
    if plan.action == "transform":
        raw["denoise"] = DENOISE[plan.strength]
    if plan.action == "extend":
        raw.update(_pads(plan.direction, ctx.source_size))

    count = plan.count if plan.action in ("generate", "transform", "likeness") else 1
    parts = [VERB[plan.action] + (f" {count}" if count > 1 else ""), ck.label]
    if "aspect" in raw:
        parts.append(plan.aspect)
    return Compiled(plan.action, workflow.key, ck.name, raw, count, " · ".join(parts))
```

- [ ] **Step 4: Run** — pass. **Step 5: Commit** `Phase 7: policy turns plan words into workflows, checkpoints and numbers`.

---

### Task 5: LLM client and prompt assembly

**Files:**
- Create: `alchemyui/planner/client.py`, `alchemyui/planner/prompt.py`
- Test: `tests/test_router.py` (client + prompt part)

**Interfaces:**
- Produces:

```python
# client.py
class LLMError(Exception):
    kind: str   # "unreachable" | "timeout" | "no_vision" | "rejected" | "bad_output"
@dataclass
class LLMReply:
    content: str; prompt_tokens: int; completion_tokens: int; ms: int
class LLMClient:
    def __init__(self, name: str, base_url: str, model: str, api_key: str = "",
                 timeout: float = 45.0, concurrency: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None): ...
    async def complete(self, messages: list[dict], schema: dict, max_tokens: int = 700) -> LLMReply
    def status(self) -> dict   # {"name","model","configured","ok","error","at"}
    async def aclose(self) -> None

# prompt.py
@dataclass(frozen=True)
class Attachment:
    handle: str; data_url: str
def thumbnail(png_or_jpeg: bytes, edge: int) -> str        # data:image/jpeg;base64,...
def system_prompt(vocab: Vocabulary) -> str
def build_messages(vocab, text: str, attachments: list[Attachment], context: list[str],
                   has_likeness: bool, answers: list[dict], images: bool = True) -> list[dict]
def repair_message(error: str) -> dict
```

- [ ] **Step 1: Failing tests** (in `tests/test_router.py`):

```python
import httpx, json, pytest
from alchemyui.planner.client import LLMClient, LLMError
from alchemyui.planner.prompt import Attachment, build_messages, system_prompt, thumbnail
from tests.test_planner_schema import V


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}
    return httpx.Response(status, json=body)


async def test_client_sends_schema_thinking_off_and_key():
    seen = {}
    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return _reply('{"ok": 1}')
    c = LLMClient("primary", "http://llm/v1", "m", api_key="k",
                  transport=httpx.MockTransport(handler))
    r = await c.complete([{"role": "user", "content": "hi"}], {"type": "object"})
    assert r.content == '{"ok": 1}' and r.prompt_tokens == 50 and r.completion_tokens == 20
    b = seen["body"]
    assert seen["auth"] == "Bearer k" and b["model"] == "m"
    assert b["response_format"]["type"] == "json_schema"
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert c.status()["ok"] is True


@pytest.mark.parametrize("response, kind", [
    (httpx.Response(500, json={"detail": "upstream 500: image input is not supported - hint"}), "no_vision"),
    (httpx.Response(502, text="bad gateway"), "unreachable"),
    (httpx.Response(401, json={"detail": "bad key"}), "rejected"),
    (httpx.Response(200, json={"choices": []}), "bad_output"),
])
async def test_client_classifies_failures(response, kind):
    c = LLMClient("p", "http://llm/v1", "m", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(LLMError) as e:
        await c.complete([], {})
    assert e.value.kind == kind
    assert c.status()["ok"] is False


async def test_client_connect_failure_is_unreachable():
    def boom(req):
        raise httpx.ConnectError("refused")
    c = LLMClient("p", "http://llm/v1", "m", transport=httpx.MockTransport(boom))
    with pytest.raises(LLMError) as e:
        await c.complete([], {})
    assert e.value.kind == "unreachable"


def test_thumbnail_is_a_small_jpeg():
    import base64, io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (2000, 1000), "red").save(buf, "PNG")
    url = thumbnail(buf.getvalue(), 768)
    assert url.startswith("data:image/jpeg;base64,")
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert im.size == (768, 384)


def test_messages_label_each_attachment_and_carry_context():
    msgs = build_messages(V, "put me in it", [Attachment("a1", "data:x"), Attachment("a2", "data:y")],
                          ["Turn 1: \"a bed\" -> generate. Images: t1.1"], True,
                          [{"question": "Which one?", "answer": "the first"}])
    assert msgs[0]["role"] == "system" and "transform" in msgs[0]["content"]
    parts = msgs[1]["content"]
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert any("a1:" in t for t in texts) and any("a2:" in t for t in texts)
    assert [p["image_url"]["url"] for p in parts if p["type"] == "image_url"] == ["data:x", "data:y"]
    blob = " ".join(texts)
    assert "t1.1" in blob and "saved likeness: yes" in blob and "the first" in blob


def test_text_only_messages_describe_but_do_not_send_images():
    msgs = build_messages(V, "x", [Attachment("a1", "data:x")], [], False, [], images=False)
    assert all(p["type"] == "text" for p in msgs[1]["content"])
    assert "a1" in msgs[1]["content"][0]["text"]


def test_system_prompt_lists_only_the_vocabulary():
    sp = system_prompt(V)
    for a in V.actions:
        assert a in sp
    assert "edit_region" not in sp
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** `client.py`:

```python
"""One OpenAI-compatible chat backend, used with schema-constrained output.

Llamacracy's /v1 is a raw passthrough to llama-server, so this same client
talks to it and to the local fallback. Failures are classified, because the
router treats them differently: an unreachable backend is skipped, a backend
that cannot see images is skipped only for turns that have images, and a
malformed reply earns one repair attempt.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..db import now


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class LLMReply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    ms: int


class LLMClient:
    def __init__(self, name: str, base_url: str, model: str, api_key: str = "",
                 timeout: float = 45.0, concurrency: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.name, self.model = name, model
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A short connect timeout: an unreachable box should fail over in
        # seconds, while a slow answer is allowed the whole budget.
        self._http = httpx.AsyncClient(
            headers=headers, transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._ok: bool | None = None
        self._error = ""
        self._at: float | None = None

    def _mark(self, ok: bool, error: str = "") -> None:
        self._ok, self._error, self._at = ok, error, now()

    def status(self) -> dict:
        return {"name": self.name, "model": self.model, "configured": True,
                "ok": self._ok, "error": self._error, "at": self._at}

    async def complete(self, messages: list[dict], schema: dict,
                       max_tokens: int = 700) -> LLMReply:
        body = {
            "model": self.model, "messages": messages, "temperature": 0.2,
            "max_tokens": max_tokens, "stream": False,
            # Thinking adds latency and nothing to a classification.
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "plan", "strict": True, "schema": schema}},
        }
        t0 = time.monotonic()
        async with self._sem:
            try:
                r = await self._http.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TimeoutException as e:
                self._mark(False, "timed out")
                raise LLMError("timeout", f"{self.name} timed out") from e
            except httpx.HTTPError as e:
                self._mark(False, f"unreachable: {type(e).__name__}")
                raise LLMError("unreachable", f"{self.name} unreachable ({e})") from e
        ms = round((time.monotonic() - t0) * 1000)

        if r.status_code >= 400:
            text = r.text[:500]
            if "image input is not supported" in text or "mmproj" in text:
                # The box is up; this model just cannot see. Not a health failure.
                self._mark(True)
                raise LLMError("no_vision", f"{self.name}: this model cannot read images")
            kind = "rejected" if r.status_code < 500 else "unreachable"
            self._mark(False, f"HTTP {r.status_code}")
            raise LLMError(kind, f"{self.name}: HTTP {r.status_code}: {text}")
        try:
            d = r.json()
            content = d["choices"][0]["message"]["content"] or ""
            usage = d.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self._mark(False, "malformed response")
            raise LLMError("bad_output", f"{self.name}: malformed response") from e
        self._mark(True)
        return LLMReply(content, int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0), ms)

    async def aclose(self) -> None:
        await self._http.aclose()
```

`prompt.py`:

```python
"""What the router is told. Plain words, one screen long: a 4B model follows
a short, concrete brief far better than a long one."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from .schema import Vocabulary

MEANING = {
    "generate": "make a brand-new image from words alone (no source image)",
    "transform": "restyle or change the whole of an existing image: 'make it darker', "
                 "'as an oil painting', 'add snow'",
    "enhance": "sharpen, upscale or fix faces in an existing image, keeping it the same",
    "likeness": "a new image of a person whose face we have: 'me as an astronaut'",
    "extend": "make an existing image bigger by painting beyond its edges",
    "select": "the user only picks which earlier image to work from next ('use the second one')",
    "ask": "you cannot tell what they want, or they need something we cannot do yet",
}


@dataclass(frozen=True)
class Attachment:
    handle: str
    data_url: str


def thumbnail(data: bytes, edge: int) -> str:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((edge, edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def system_prompt(vocab: Vocabulary) -> str:
    actions = "\n".join(f"- {a}: {MEANING[a]}" for a in vocab.actions)
    return f"""You plan image requests for an image generator. Reply with exactly one JSON plan.

Actions:
{actions}

Images are named by handles: a1, a2... are this message's attachments in order;
t4.2 is the second image made by turn 4 of this conversation; "me" is the user's
saved face. Only use handles you are given.

Rules:
- source: the image to work from (transform, enhance, extend, select). "" for generate.
- faces: for likeness, "me" or the attachment that shows their face. "" otherwise.
- prompt: rewrite the request as a Stable Diffusion prompt in English, comma-separated,
  under 60 words: subject first, then setting, lighting, style, "photo" details. For
  transform, describe the whole finished image, not just the change.
- negative: a few things to avoid, or "".
- strength: how much a transform changes the image. subtle, medium or strong.
- quality: "fast" unless they ask for best, highest, detailed or professional quality.
- count: how many images they asked for (default 1).
- aspect: {', '.join(vocab.aspects)}. Portrait subjects 3:4, landscapes 16:9, else 1:1.
- direction: for extend only: {', '.join(d for d in ('wider', 'taller', 'left', 'right', 'up', 'down', 'all'))}.
- question and options: for ask only; up to 4 short options. Otherwise "" and [].
- "make it darker" and other follow-ups work on the current image. If the last turn
  made several images and they have not picked one, ask which.
- If they want part of a photo changed or someone added into a specific photo, you
  cannot do that yet: ask, and offer a whole-image alternative."""


def build_messages(vocab: Vocabulary, text: str, attachments: list[Attachment],
                   context: list[str], has_likeness: bool, answers: list[dict],
                   images: bool = True) -> list[dict]:
    lines = []
    if context:
        lines.append("Conversation so far:")
        lines.extend(context)
    lines.append(f"The user has a saved likeness: {'yes' if has_likeness else 'no'}.")
    if attachments:
        lines.append("This message has " + ", ".join(a.handle for a in attachments)
                     + " attached" + ("" if images else " (you cannot see them)") + ".")
    lines.append(f'The user says: "{text.strip()}"' if text.strip()
                 else "The user sent only the attachments, with no words.")
    for qa in answers:
        lines.append(f'You asked: "{qa["question"]}" They answered: "{qa["answer"]}"')
    parts: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    if images:
        for a in attachments:
            parts.append({"type": "text", "text": f"{a.handle}:"})
            parts.append({"type": "image_url", "image_url": {"url": a.data_url}})
    return [{"role": "system", "content": system_prompt(vocab)},
            {"role": "user", "content": parts}]


def repair_message(error: str) -> dict:
    return {"role": "user", "content": f"That plan was rejected: {error}. "
                                       "Reply with a corrected plan."}
```

- [ ] **Step 4: Run** — pass. **Step 5: Commit** `Phase 7: LLM client with classified failures, and the router's prompt`.

---

### Task 6: Router — primary, repair, fallback, default

**Files:**
- Create: `alchemyui/planner/router.py`
- Test: `tests/test_router.py` (router part)

**Interfaces:**
- Produces:

```python
@dataclass
class RouteRequest:
    text: str
    attachments: list[Attachment]
    context: list[str]
    has_likeness: bool
    answers: list[dict]
    sources: list[str]          # handles allowed as source
    faces: list[str]            # handles allowed as faces

@dataclass
class RouteResult:
    plan: Plan | None           # None when the default planner must ask
    source: str                 # "primary" | "fallback" | "default"
    ms: int
    prompt_tokens: int
    completion_tokens: int
    errors: list[str]
    question: dict | None = None   # default planner's ask, with plans on its options

class Router:
    def __init__(self, vocab: Vocabulary, primary: LLMClient | None, fallback: LLMClient | None): ...
    async def route(self, req: RouteRequest) -> RouteResult
    def status(self) -> dict     # {"primary": {...}|None, "fallback": {...}|None}

def default_plan(req: RouteRequest, vocab: Vocabulary) -> tuple[Plan | None, dict | None]
```

- [ ] **Step 1: Failing tests** (append to `tests/test_router.py`):

```python
from alchemyui.planner.router import Router, RouteRequest

GOOD = json.dumps(dict(action="generate", source="", faces="", prompt="lighthouse, night",
                       negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                       direction="", question="", options=[]))


class Scripted:
    """An LLMClient stand-in that replays recorded replies or raises."""
    def __init__(self, name, *script):
        self.name, self.script, self.calls = name, list(script), []
    async def complete(self, messages, schema, max_tokens=700):
        from alchemyui.planner.client import LLMReply
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return LLMReply(step, 100, 30, 5)
    def status(self):
        return {"name": self.name}


def req(text="a spooky lighthouse", attachments=(), sources=(), faces=()):
    return RouteRequest(text, list(attachments), [], False, [], list(sources), list(faces))


async def test_primary_plan_is_used():
    p = Scripted("primary", GOOD)
    r = await Router(V, p, Scripted("fallback")).route(req())
    assert r.source == "primary" and r.plan.action == "generate" and r.errors == []


async def test_invalid_plan_gets_one_repair_with_the_error():
    bad = GOOD.replace('"generate"', '"transform"')
    p = Scripted("primary", bad, GOOD)
    r = await Router(V, p, None).route(req())
    assert r.source == "primary" and r.plan.action == "generate"
    assert "needs a source image" in p.calls[1][-1]["content"]


async def test_unreachable_primary_falls_back():
    p = Scripted("primary", LLMError("unreachable", "down"))
    f = Scripted("fallback", GOOD)
    r = await Router(V, p, f).route(req())
    assert r.source == "fallback" and "down" in r.errors[0]


async def test_no_vision_primary_hands_image_turns_to_the_fallback():
    p = Scripted("primary", LLMError("no_vision", "cannot see"))
    f = Scripted("fallback", GOOD.replace('"generate", "source": ""', '"transform", "source": "a1"'))
    r = await Router(V, p, f).route(req(attachments=[Attachment("a1", "data:x")], sources=["a1"]))
    assert r.source == "fallback" and r.plan.action == "transform"


async def test_both_down_text_only_generates_the_users_words():
    r = await Router(V, Scripted("p", LLMError("unreachable", "x")),
                     Scripted("f", LLMError("timeout", "y"))).route(req("Snowy Forest during Snowstorm"))
    assert r.source == "default" and r.plan.action == "generate"
    assert r.plan.prompt == "Snowy Forest during Snowstorm"


async def test_both_down_with_attachments_asks_with_ready_made_options():
    r = await Router(V, None, None).route(
        req("x", attachments=[Attachment("a1", "data:x")], sources=["a1"]))
    assert r.source == "default" and r.plan is None
    labels = [o["label"] for o in r.question["options"]]
    assert labels[:3] == ["Restyle it", "Enhance it", "Make it wider"]
    assert all(o["plan"]["source"] == "a1" for o in r.question["options"])


async def test_twice_invalid_moves_on_to_the_fallback():
    p = Scripted("primary", "nope", "still nope")
    f = Scripted("fallback", GOOD)
    r = await Router(V, p, f).route(req())
    assert r.source == "fallback" and len(p.calls) == 2
```

- [ ] **Step 2: Run** — FAIL.

- [ ] **Step 3: Implement** `router.py`:

```python
"""Primary, then fallback, then a plan made of the user's own words.

Each backend gets one repair retry for a plan that parses but does not hold
together, with the validation error fed back verbatim. An unreachable or
timed-out backend is skipped at once; one that cannot see images is skipped
only for turns that carry images, and the text-only turns keep using it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .client import LLMError
from .prompt import Attachment, build_messages, repair_message
from .schema import Plan, PlanInvalid, Vocabulary, plan_schema, validate_plan


@dataclass
class RouteRequest:
    text: str
    attachments: list[Attachment]
    context: list[str]
    has_likeness: bool
    answers: list[dict]
    sources: list[str]
    faces: list[str]


@dataclass
class RouteResult:
    plan: Plan | None
    source: str
    ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    question: dict | None = None


def _plan(vocab: Vocabulary, **kw) -> Plan:
    base = dict(action="generate", source="", faces="", prompt="", negative="",
                aspect=vocab.aspects[0], count=1, strength="medium", quality="fast",
                direction="", question="", options=())
    base.update(kw)
    return Plan(**base)


def default_plan(req: RouteRequest, vocab: Vocabulary) -> tuple[Plan | None, dict | None]:
    """No model answered. Text alone is taken literally as a prompt; with an
    image we cannot know what was meant, so we offer what the image allows."""
    text = req.text.strip()
    if not req.attachments:
        return _plan(vocab, prompt=text or "a beautiful photograph"), None
    src = req.attachments[0].handle
    offers = [("transform", "Restyle it", {"strength": "medium"}),
              ("enhance", "Enhance it", {}),
              ("extend", "Make it wider", {"direction": "wider"})]
    options = []
    for action, label, extra in offers:
        if action in vocab.actions:
            options.append({"label": label, "plan": _plan(
                vocab, action=action, source=src, prompt=text or "high quality photo",
                **extra).as_dict()})
    return None, {"question": "The planner is offline, so I can't tell what you'd like done "
                              "with that image. Pick one:", "options": options}


class Router:
    def __init__(self, vocab: Vocabulary, primary, fallback):
        self.vocab, self.primary, self.fallback = vocab, primary, fallback

    def status(self) -> dict:
        return {"primary": self.primary.status() if self.primary else None,
                "fallback": self.fallback.status() if self.fallback else None}

    async def route(self, req: RouteRequest) -> RouteResult:
        result = RouteResult(None, "default")
        schema = plan_schema(self.vocab, req.sources, req.faces)
        for name, client in (("primary", self.primary), ("fallback", self.fallback)):
            if client is None:
                continue
            messages = build_messages(self.vocab, req.text, req.attachments, req.context,
                                      req.has_likeness, req.answers)
            for attempt in range(2):
                try:
                    reply = await client.complete(messages, schema)
                except LLMError as e:
                    result.errors.append(f"{name}: {e}")
                    break
                result.ms += reply.ms
                result.prompt_tokens += reply.prompt_tokens
                result.completion_tokens += reply.completion_tokens
                try:
                    result.plan = validate_plan(reply.content, self.vocab, req.sources, req.faces)
                    result.source = name
                    return result
                except PlanInvalid as e:
                    result.errors.append(f"{name}: invalid plan ({e})")
                    if attempt == 0:
                        messages = [*messages, {"role": "assistant", "content": reply.content},
                                    repair_message(str(e))]
        result.plan, result.question = default_plan(req, self.vocab)
        return result
```

- [ ] **Step 4: Run** — pass. **Step 5: Commit** `Phase 7: router with repair, fallback and a no-LLM default`.

---

### Task 7: Turn engine

**Files:**
- Create: `alchemyui/threads.py`
- Modify: `alchemyui/app.py` (lifespan builds `Router` + `ChatEngine`), `tests/conftest.py` (`FakeRouter`, engine wiring)
- Test: `tests/test_threads.py`

**Interfaces:**
- Consumes: `submit_jobs`, `image_as_upload` (Task 2); `QueueManager.add_listener/emit`; `Router`, `RouteRequest`, `Attachment`, `thumbnail`; `compile_plan`, `PolicyContext`, `NeedsInput`; `normalise_reference`, `get_analyser`.
- Produces:

```python
class TurnState(enum.StrEnum):
    PLANNING = "planning"; NEEDS_INPUT = "needs_input"; QUEUED = "queued"
    DONE = "done"; FAILED = "failed"; CANCELLED = "cancelled"

class TurnError(Exception):            # -> HTTP 400 with str(e); status attr for 404/409
    def __init__(self, message: str, status: int = 400): ...

class ChatEngine:
    def __init__(self, db, qm, reg, settings, router, comfy): ...
    async def start(self) -> None        # listener + reconcile
    async def stop(self) -> None
    async def list_threads(self, user_id) -> list[dict]
    async def get_thread(self, user_id, thread_id) -> dict            # {"thread", "turns"}
    async def delete_thread(self, user_id, thread_id) -> None
    async def create_turn(self, user_id, text, attachment_ids, thread_id=None) -> dict
    async def answer(self, user_id, turn_id, text: str | None, option: int | None) -> dict
    async def cancel(self, user_id, turn_id) -> dict
    async def retry(self, user_id, turn_id) -> dict
    async def turn_public(self, row) -> dict
    def status(self) -> dict                 # {"router": router.status(), "planning": n}

get_engine() -> ChatEngine; start_engine(...); stop_engine()
```

Turn public payload (the frontend contract):

```json
{"id": "...", "thread_id": "...", "seq": 4, "text": "...",
 "attachments": [{"handle": "a1", "id": "<upload id>", "url": "/api/uploads/<id>/file"}],
 "state": "queued", "chip": "Generating · Realistic Vision · 3:4",
 "plan": {...Plan.as_dict()...} | null, "compiled": {...} | null,
 "plan_source": "primary", "plan_error": null, "reply": null,
 "question": {"question": "...", "options": ["label", ...]} | null,
 "answers": [...], "router_ms": 812,
 "jobs": [Job.public()...], "images": [{..._image_row.., "handle": "t4.1"}],
 "created_at": 0, "finished_at": null}
```

Behaviour to implement (each bullet has a test below):
1. `create_turn`: validates ≤4 attachments, each an upload owned by the user (else `TurnError(404)`); needs text or attachments; creates the thread when `thread_id` is None (title = first 60 chars of text, or "Images" when none); `seq = max+1`; state `planning`; bumps `threads.updated_at`; emits `{"type":"turn","turn":...}` to the owner; starts `_plan(turn_id)` as a tracked task; returns the public turn.
2. **Scope** (`_scope(turn)`): attachments → `a1..a4` (upload rows). Outputs of earlier turns in the same thread and owned by the user, ordered by `seq` then image `created_at` → `t{seq}.{n}`; only the last `router_context_turns` turns are offered as handles. `me` offered as a face when the user has ≥1 `is_likeness=1` ref. `faces` handles = `me` + attachment handles.
3. **Context lines**: per earlier turn: `Turn {seq}: "{text[:120]}" -> {action}{ from source}. Images: t{seq}.1, t{seq}.2 (prompt: "{prompt[:120]}")`. Then a focus line: the latest `select` target, else the latest image-producing turn: one image → `Current image: t5.1.`; several → `Latest images: t5.1–t5.4 (none picked yet).`
4. `_plan`: build `RouteRequest` (thumbnails read from the upload files; attachments' pixels only — earlier outputs are never sent), `await router.route`, record `plan_source`, `plan_error` ("; ".join(errors) or None), `router_ms`, tokens, `planned_at`. Then `_execute`.
5. `_execute(turn, plan)`: resolve `plan.source` → an upload row (attachment directly, `tN.M` via `image_as_upload`); `PolicyContext(qm._resident_checkpoint, (w,h) of the source upload, has_likeness)`; `compile_plan`.
   - `NeedsInput` → state `needs_input`, `question_json`, `reply = question`.
   - `select` → state `done`, `reply = "Okay, working from image N of turn S."`, `plan_json` stored (this is what the focus line reads).
   - Likeness: `faces == "me"` → the user's likeness ref comfy names (max 4); `faces == aN` → `normalise_reference` on that upload's file, stored as a `refs` row with `is_likeness = 0`; `FaceRejected` → `needs_input` with its message.
   - Otherwise `submit_jobs(..., count=compiled.count, upload=source upload, turn_id)`; state `queued`. `JobRejected` → `failed` with the message as `reply`.
   - `plan_json = {"plan": plan.as_dict(), "compiled": compiled.as_dict(), "chip": compiled.chip}`; if `plan_source == "default"` the chip gains the suffix ` · planner offline`, and ` · backup planner` for `fallback`.
6. **Job hook**: on every finished job with a `turn_id`: when all the turn's jobs are terminal, state becomes `done` if any completed, `cancelled` if the turn was cancelled, else `failed` (reply = first job's `error_text`); `finished_at` set; emit.
7. `answer`: only in `needs_input`. With `option` whose stored option has a `plan` → execute that plan directly (source `default`). Otherwise append `{"question", "answer": text or option label}` to `answers_json` and re-plan (state `planning`).
8. `cancel`: planning → task cancelled, state `cancelled`; needs_input → `cancelled`; queued → `qm.cancel` each live job, mark the turn `cancelled` (the hook keeps it there). Terminal → `TurnError(409)`.
9. `retry`: only on a terminal turn with a compiled plan that has a workflow: re-submit with fresh seeds (`raw` without `seed`), state back to `queued`.
10. `start()` reconcile: `planning` turns are re-planned; `queued` turns whose jobs are all terminal are finalised by the hook logic; `needs_input` untouched.
11. Every SQL filters on `user_id`; another user's turn or thread is `TurnError(404)`.

- [ ] **Step 1: Failing tests** `tests/test_threads.py` (HTTP-level through the API is Task 8; these drive `ChatEngine` directly over the real queue and `FakeComfy`, with a `FakeRouter` returning scripted `RouteResult`s):

```python
"""The turn engine: planning, handles, needs_input, the job hook, restart."""
import asyncio, json
import pytest
from alchemyui.planner.router import RouteResult
from alchemyui.planner.schema import Plan
from tests.conftest import add_user, engine_harness   # engine_harness added to conftest in this task


def P(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="1:1", count=1, strength="medium", quality="fast", direction="",
                question="", options=())
    base.update(kw)
    return Plan(**base)


async def settle(h, turn_id, states=("done", "failed", "needs_input", "cancelled"), timeout=5):
    for _ in range(int(timeout / 0.02)):
        t = await h.engine.turn_public(await h.row(turn_id))
        if t["state"] in states:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn stuck in {t['state']}")


async def test_text_turn_plans_queues_and_finishes(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=2), "primary", 900, 120, 40)]
    t = await h.engine.create_turn(h.alice, "spooky lighthouse", [])
    assert t["state"] == "planning" and t["seq"] == 1
    done = await settle(h, t["id"])
    assert done["state"] == "done" and len(done["jobs"]) == 2
    assert [i["handle"] for i in done["images"]] == ["t1.1", "t1.2"]
    assert done["chip"].startswith("Generating 2") and done["plan_source"] == "primary"
    assert done["router_ms"] == 900


async def test_follow_up_sees_earlier_outputs_as_handles_but_never_their_pixels(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary"),
                        RouteResult(P(action="transform", source="t1.1", strength="subtle"), "primary")]
    t1 = await h.engine.create_turn(h.alice, "a lighthouse", [])
    await settle(h, t1["id"])
    t2 = await h.engine.create_turn(h.alice, "make it darker", [], thread_id=t1["thread_id"])
    done = await settle(h, t2["id"])
    req = h.router.requests[1]
    assert "t1.1" in req.sources and req.attachments == []
    assert any("Current image: t1.1" in line for line in req.context)
    assert done["state"] == "done" and done["jobs"][0]["params"]["denoise"] == 0.35


async def test_another_users_images_are_never_in_scope(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary"), RouteResult(P(), "primary")]
    a = await h.engine.create_turn(h.alice, "alice's", [])
    await settle(h, a["id"])
    with pytest.raises(Exception, match="no such thread"):
        await h.engine.create_turn(h.bob, "steal", [], thread_id=a["thread_id"])
    b = await h.engine.create_turn(h.bob, "bob's", [])
    await settle(h, b["id"])
    assert h.router.requests[1].sources == []


async def test_attachment_of_another_user_is_refused(engine_harness):
    h = engine_harness
    up = await h.upload(h.alice)
    with pytest.raises(Exception, match="no such upload"):
        await h.engine.create_turn(h.bob, "mine now", [up])


async def test_ask_parks_without_a_queue_place_and_an_answer_replans(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(action="ask", question="Which one?", options=("1", "2")), "primary"),
                        RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "hmm", [])
    parked = await settle(h, t["id"])
    assert parked["state"] == "needs_input" and parked["jobs"] == []
    assert parked["question"] == {"question": "Which one?", "options": ["1", "2"]}
    assert h.qm.snapshot()["depth"] == 0
    await h.engine.answer(h.alice, t["id"], None, 1)
    done = await settle(h, t["id"], ("done",))
    assert h.router.requests[1].answers == [{"question": "Which one?", "answer": "2"}]
    assert done["state"] == "done"


async def test_default_planner_options_run_without_the_llm(engine_harness):
    h = engine_harness
    up = await h.upload(h.alice)
    plan = P(action="enhance", source="a1").as_dict()
    h.router.results = [RouteResult(None, "default", question={
        "question": "Pick one:", "options": [{"label": "Enhance it", "plan": plan}]})]
    t = await h.engine.create_turn(h.alice, "", [up])
    await settle(h, t["id"])
    await h.engine.answer(h.alice, t["id"], None, 0)
    done = await settle(h, t["id"], ("done", "failed"))
    assert len(h.router.requests) == 1 and done["jobs"][0]["workflow_id"] == "enhance-sdxl"


async def test_likeness_without_saved_faces_asks(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(action="likeness", faces="me"), "primary")]
    t = await h.engine.create_turn(h.alice, "me as an astronaut", [])
    parked = await settle(h, t["id"])
    assert parked["state"] == "needs_input" and "likeness" in parked["question"]["question"]


async def test_likeness_uses_the_saved_likeness(engine_harness):
    h = engine_harness
    await h.likeness(h.alice, n=2)
    h.router.results = [RouteResult(P(action="likeness", faces="me"), "primary")]
    t = await h.engine.create_turn(h.alice, "me as an astronaut", [])
    done = await settle(h, t["id"])
    assert "me" in h.router.requests[0].faces
    assert len(done["jobs"][0]["params"]["references"]) == 2


async def test_select_moves_focus_and_queues_nothing(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=2), "primary"),
                        RouteResult(P(action="select", source="t1.2"), "primary"),
                        RouteResult(P(action="transform", source="t1.2"), "primary")]
    t1 = await h.engine.create_turn(h.alice, "two foxes", [])
    await settle(h, t1["id"])
    t2 = await h.engine.create_turn(h.alice, "use the second one", [], thread_id=t1["thread_id"])
    sel = await settle(h, t2["id"])
    assert sel["state"] == "done" and sel["jobs"] == [] and "image 2" in sel["reply"]
    t3 = await h.engine.create_turn(h.alice, "now add a dog", [], thread_id=t1["thread_id"])
    await settle(h, t3["id"])
    assert any("Current image: t1.2" in line for line in h.router.requests[2].context)


async def test_several_new_images_are_ambiguous_to_the_router(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=3), "primary"), RouteResult(P(), "primary")]
    t1 = await h.engine.create_turn(h.alice, "three foxes", [])
    await settle(h, t1["id"])
    await h.engine.create_turn(h.alice, "make it darker", [], thread_id=t1["thread_id"])
    await asyncio.sleep(0.1)
    assert any("none picked yet" in line for line in h.router.requests[1].context)


async def test_cancel_a_queued_turn_cancels_its_jobs(engine_harness):
    h = engine_harness
    h.comfy.mode = "hang"
    h.router.results = [RouteResult(P(count=2), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    await settle(h, t["id"], ("queued",))
    await h.engine.cancel(h.alice, t["id"])
    done = await settle(h, t["id"], ("cancelled",))
    assert {j["status"] for j in done["jobs"]} <= {"cancelled", "running"}
    assert h.comfy.interrupts


async def test_failed_jobs_fail_the_turn_with_the_reason(engine_harness):
    h = engine_harness
    h.comfy.mode = "error"
    h.router.results = [RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    done = await settle(h, t["id"])
    assert done["state"] == "failed" and "KSampler" in done["reply"]


async def test_restart_replans_turns_left_planning(engine_harness):
    h = engine_harness
    h.router.hold = True                        # planning never returns
    t = await h.engine.create_turn(h.alice, "x", [])
    await h.engine.stop()
    h.router.hold = False
    h.router.results = [RouteResult(P(), "primary")]
    await h.restart_engine()
    done = await settle(h, t["id"])
    assert done["state"] == "done"


async def test_retry_runs_the_same_plan_with_new_seeds(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    first = await settle(h, t["id"])
    await h.engine.retry(h.alice, t["id"])
    again = await settle(h, t["id"], ("done",), timeout=5)
    await asyncio.sleep(0.1)
    again = await h.engine.turn_public(await h.row(t["id"]))
    assert len(again["jobs"]) == 2 and len(h.router.requests) == 1
    seeds = {j["params"]["seed"] for j in again["jobs"]}
    assert len(seeds) == 2 and [i["handle"] for i in again["images"]] == ["t1.1", "t1.2"]
```

`tests/conftest.py` additions:

```python
class FakeRouter:
    """Replays scripted RouteResults and records every RouteRequest."""
    def __init__(self):
        self.results, self.requests, self.hold = [], [], False
    async def route(self, req):
        self.requests.append(req)
        while self.hold:
            await asyncio.sleep(0.01)
        return self.results.pop(0)
    def status(self):
        return {"primary": None, "fallback": None}


@pytest.fixture
async def engine_harness(tmp_db_path, tmp_output_dir, monkeypatch):
    """A started QueueManager over FakeComfy plus a ChatEngine over FakeRouter,
    with two users. `upload(uid)` records a real PNG upload; `likeness(uid, n)`
    stores n likeness refs; `restart_engine()` builds a fresh engine on the same
    DB and queue."""
    ...  # build Database, FakeComfy, load_registry, QueueManager(sampler_factory=FixedSampler),
         # ChatEngine(db, qm, reg, settings, FakeRouter(), comfy); start both; yield a
         # SimpleNamespace(engine, qm, comfy, db, router, alice, bob, row, upload,
         # likeness, restart_engine); stop both.
```

- [ ] **Step 2: Run** — FAIL (no module).
- [ ] **Step 3: Implement** `threads.py` to the behaviour list above; `app.py` lifespan: after `start_queue()`, build the router (`_build_router(settings, reg)` → `Router(Vocabulary.from_registry(reg, settings.max_batch), primary if url and model else None, fallback if url else None)`), `await start_engine(...)`; on shutdown `await stop_engine()` before `stop_queue()`, then close the LLM clients.
- [ ] **Step 4: Run** `uv run pytest -q` — pass.
- [ ] **Step 5: Commit** `Phase 7: turn engine -- threads, handles in the user's own scope, needs_input, the job hook`.

---

### Task 8: Chat API, likeness settings, admin router status

**Files:**
- Create: `alchemyui/chat.py`
- Modify: `alchemyui/app.py` (include router; `/api/references` lists only `is_likeness = 1` and refuses a fifth), `alchemyui/admin.py`, `alchemyui/static/assets/admin.js`
- Test: `tests/test_chat_api.py`, `tests/test_admin.py`

**Interfaces:**
- Routes (all `get_principal`; `TurnError` → its status):
  - `GET /api/threads` → `{"threads": [{"id","title","updated_at"}]}` newest first, not deleted.
  - `GET /api/threads/{id}` → `{"thread": {...}, "turns": [turn public...]}`.
  - `DELETE /api/threads/{id}` → `{"deleted": id}`.
  - `POST /api/turns` body `{"thread_id": str|null, "text": str (≤4000), "attachments": [upload id] (≤4)}` → turn public.
  - `POST /api/turns/{id}/answer` body `{"text": str|null, "option": int|null}`.
  - `POST /api/turns/{id}/cancel`, `POST /api/turns/{id}/retry`.
  - `GET /api/admin/live` gains `"router": engine.status()`.
  - `GET /api/admin/fallbacks?limit=` → `{"fallbacks": [{"turn_id","owner","plan_source","plan_error","created_at"}]}` — **no text, no prompt**.

- [ ] **Step 1: Failing tests** `tests/test_chat_api.py` (`client` fixture with `DEV_MODE=""` and the `A`/`B` headers; the lifespan-built engine's router is replaced by `FakeRouter` through a new conftest hook `client.router`):

```python
pytestmark = pytest.mark.env(DEV_MODE="")

def test_turn_round_trip_over_http(client): ...
    # POST /api/turns -> planning; wait_for GET /api/threads/{id} turns[0].state == "done";
    # GET /api/threads lists it with the title from the text.

def test_threads_are_private(client): ...
    # A's thread: B gets 404 on GET/DELETE /api/threads/{id}, and on
    # answer/cancel/retry of A's turn; B's /api/threads is empty.

def test_attachments_limit_and_ownership(client): ...
    # 5 attachments -> 400; B attaching A's upload -> 404; empty text + no attachments -> 400.

def test_likeness_is_capped_at_four_and_lists_only_likenesses(client, monkeypatch): ...
    # monkeypatch app.normalise_reference to accept anything; 4 POST /api/references
    # succeed, the 5th is 400 "up to 4"; a refs row inserted with is_likeness=0
    # does not appear in GET /api/references.

def test_admin_sees_router_status_and_fallbacks_without_text(client): ...
    # admin GET /api/admin/live has "router"; a turn planned with plan_source
    # "fallback" appears in /api/admin/fallbacks with owner and plan_error, and
    # the response body contains neither the turn text nor the prompt.
```

(Write each test fully in the file; the comments above are the assertions each one makes.)

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** `chat.py` as a thin `APIRouter(prefix="/api")` over `get_engine()`; admin additions; `admin.js` renders a "Router" tile (primary: model + ok/unreachable/cannot see images; fallback: ok/down) and a "Recent fallbacks" list under failures.
- [ ] **Step 4: Run** `uv run pytest -q` — pass. **Step 5: Commit** `Phase 7: chat API, likeness settings, router status on the admin page`.

---

### Task 9: The new SPA

**Files:**
- Rewrite: `alchemyui/static/assets/app.js`, `alchemyui/static/assets/styles.css` (keep the token block and base; replace components), `alchemyui/static/index.html` (meta description only)
- Modify: `tests/test_frontend.py`, `tests/js/boot.mjs` (only if a new DOM call needs shimming)
- Test: `tests/test_frontend.py`

**Contract (spec §12):**
- **Routes** (hash): `#/` new chat, `#/t/<thread id>`, `#/library`, `#/settings`.
- **Rail:** "New chat", threads newest first (title, relative time; delete on hover), "Library", avatar menu (Settings, Admin if `me.is_admin`, Sign out → `/oauth2/sign_out`). Below 820px wide the rail is a drawer behind a menu button.
- **Thread view:** per turn, a user bubble (text + numbered attachment thumbs "1", "2"… matching a1, a2) and an assistant block:
  - **plan chip** (`turn.chip`, or "Thinking…" while `planning`, with elapsed seconds); tapping it opens details: rewritten prompt, negative, model label, seed(s), plan source, router ms, and **Retry** (terminal turns with jobs).
  - `turn.reply` text when present.
  - **progress cards** for the turn's live jobs — v1's card: "N ahead of you" → step count, elapsed, ETA → live preview frame.
  - **images** grid with `t4.2` badges; each opens the lightbox (enlarge, download, favourite, "Use in next message" → `POST /api/images/{id}/reuse` → attachment chip in the composer).
  - **needs_input:** the question and option buttons (`POST /answer {option}`); typing in the composer while the newest turn is `needs_input` answers it instead of creating a turn (`POST /answer {text}`).
  - failed turn: `reply` in a danger note; job `error_detail` behind `<details>`.
- **Composer:** one auto-growing textarea; paperclip, drag-and-drop and paste (≤4 images; each uploads immediately via `POST /api/uploads` and shows a numbered chip with remove); Enter sends, Shift+Enter newline; while the thread's newest turn is planning or queued the send button becomes **Stop** (`POST /api/turns/{id}/cancel`). Auto-focus only on fine pointers.
- **Library:** the user's images newest first (`/api/images`), favourite filter and prompt search, infinite "Load more" via `before=`; click → lightbox.
- **Settings:** likeness photos (list, add up to 4 via `POST /api/references`, delete), with the rule shown ("1–4 clear photos of your face"), and "This month": images, kWh, cost from `/api/me/usage`, with the "estimate of the GPU's share" note.
- **SSE:** one `EventSource('/api/events')`; handle `turn` (upsert into the open thread; bump the rail), `queue`, `job`, `progress`, `preview`, `image`. On reconnect, re-fetch the open thread and `/api/threads`.
- Reuse from v1 verbatim: `api`, `err`, `h`, `ICONS`/`icon` (+ `paperclip`, `menu`, `plus`, `send`, `settings`, `logout`), `fmtDur`, `plural`, `hasFinePointer`, the preview fetch-one-at-a-time logic, the lightbox shell.

- [ ] **Step 1: Failing test** — change the parametrised expectation in `tests/test_frontend.py`: `("app.js", "/api/me")` stays, and add a second assertion for app.js that `"/api/threads"` is in `requests`, and a test that boots with `location.hash = '#/settings'` (pass the hash as a third argv to `boot.mjs`) and asserts `/api/references` and `/api/me/usage` were requested.
- [ ] **Step 2: Run** — FAIL (v1 app.js never requests `/api/threads`).
- [ ] **Step 3: Implement** the SPA to the contract.
- [ ] **Step 4: Run** `uv run pytest tests/test_frontend.py -q` then `uv run pytest -q` — pass.
- [ ] **Step 5: Commit** `Phase 7: the chat SPA replaces v1's composer`.

---

### Task 10: Fallback unit and deploy

**Files:**
- Create: `deploy/systemd/alchemyui-router.service`
- Modify: `deploy/install.sh`, `deploy/alchemyui-cli.sh`, `.env.example`
- Test: `tests/test_deploy.py`

- [ ] **Step 1: Failing test** `tests/test_deploy.py`:

```python
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
UNIT = (REPO / "deploy/systemd/alchemyui-router.service").read_text()

def test_router_unit_is_cpu_only_twice_over():
    """-ngl 0 alone is not enough: a CUDA build still offloads large prompt
    batches and the vision projector unless it cannot see a GPU at all."""
    assert "CUDA_VISIBLE_DEVICES=" in UNIT and "-ngl 0" in UNIT
    assert "--no-mmproj-offload" in UNIT and "--mmproj" in UNIT
    assert "--host 127.0.0.1" in UNIT

def test_router_unit_takes_machine_paths_from_the_env_file():
    assert "${ROUTER_FALLBACK_GGUF}" in UNIT and "${ROUTER_FALLBACK_MMPROJ}" in UNIT
    assert "/home/" not in UNIT

def test_env_example_documents_every_router_setting():
    ex = (REPO / ".env.example").read_text()
    for k in ("ROUTER_PRIMARY_URL", "ROUTER_PRIMARY_KEY", "ROUTER_PRIMARY_MODEL",
              "ROUTER_FALLBACK_URL", "ROUTER_FALLBACK_GGUF", "ROUTER_FALLBACK_MMPROJ",
              "ROUTER_TIMEOUT_S", "ROUTER_CONTEXT_TURNS", "MAX_BATCH"):
        assert k in ex, k
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the unit:

```ini
# ~/.config/systemd/user/alchemyui-router.service  (installed by deploy/install.sh)
#
# The router's local fallback: a CPU-only llama-server with the vision
# projector, used when Llamacracy is unreachable or cannot read images.
# It must never touch the GPU -- that belongs to ComfyUI -- so it is kept off
# it twice: no CUDA device is visible, and no layers or projector are
# offloaded even if one were.
[Unit]
Description=AlchemyUI router fallback (llama-server, CPU only)
After=network.target

[Service]
Type=simple
WorkingDirectory=@REPO@
EnvironmentFile=@REPO@/.env
ExecStart=/usr/bin/env CUDA_VISIBLE_DEVICES= @LLAMA_SERVER@ -m ${ROUTER_FALLBACK_GGUF} --mmproj ${ROUTER_FALLBACK_MMPROJ} -ngl 0 --no-mmproj-offload --host 127.0.0.1 --port ${ROUTER_FALLBACK_PORT} -c 8192 -t 6 --jinja --no-webui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

`install.sh`: when `.env` has `ROUTER_FALLBACK_GGUF` pointing at an existing file, install the unit with `@REPO@` and `@LLAMA_SERVER@` (`command -v llama-server`) substituted and enable it; otherwise print how to enable it later. `alchemyui-cli.sh`: add `alchemyui-router.service` to `UNITS` when installed and enabled; `status` also curls `${ROUTER_FALLBACK_URL%/v1}/health`. `.env.example`: a router block documenting every key with the E4B defaults (`ROUTER_PRIMARY_MODEL=gemma4-e4b-uncensored` as the example value, `ROUTER_FALLBACK_PORT=8081`, `ROUTER_FALLBACK_GGUF=/path/to/gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf`, `ROUTER_FALLBACK_MMPROJ=/path/to/mmproj-BF16.gguf`), plus `MAX_BATCH=4`. Add `router_fallback_gguf`, `router_fallback_mmproj`, `router_fallback_port` to `Settings` so pydantic accepts them.

Local `.env` (gitignored, not committed): add `ROUTER_PRIMARY_MODEL=gemma4-e4b-uncensored`, `ROUTER_FALLBACK_GGUF`, `ROUTER_FALLBACK_MMPROJ`, `ROUTER_FALLBACK_PORT=8081`.

- [ ] **Step 4: Run** `bash -n deploy/install.sh deploy/alchemyui-cli.sh` and `uv run pytest -q`. Do **not** run `install.sh`.
- [ ] **Step 5: Commit** `Phase 7: alchemyui-router, the CPU-only fallback unit`.

---

### Task 11: router-eval

**Files:**
- Create: `alchemyui/evals.py`, `evals/router/cases.json`, `evals/router/images/*.jpg`
- Modify: `alchemyui/cli.py`
- Test: `tests/test_evals.py`

**Interfaces:**
- `alchemyui router-eval [--backend primary|fallback|both] [--only ID] [--out DIR]` — runs every case against each backend through the real `Router` machinery for one backend at a time (`Router(vocab, client, None)` with the default planner disabled: a default result scores as a miss), prints a side-by-side table and writes `DIR/router-eval-<timestamp>.md` (default `data/router-eval/`, gitignored).
- `cases.json`: `[{"id", "text", "attachments": ["bed.jpg", ...], "context": [lines], "has_likeness": bool, "sources": [...], "faces": [...], "expect": {"action": str | [str], "source"?: str, "faces"?: str}}]`. At least 30 cases, including all five §1 examples (example 3 expects `ask`; example 4 expects `ask` until Phase 9), follow-ups with context, multi-image ambiguity, likeness with and without a saved likeness, extend, enhance, select, count, and quality words.
- Images: generated once on this laptop with ComfyUI (RV6, 512px): `person.jpg` (a head-and-shoulders portrait), `bed.jpg`, `beach.jpg`, `dog.jpg`, `sweatpants.jpg`, `street.jpg`; saved as JPEG q80 at ≤512px. AI-generated, safe to commit.
- `score(case, plan) -> (ok: bool, why: str)`; `run_eval(cases, clients) -> dict`; `render_report(results) -> str`.

- [ ] **Step 1: Failing tests** `tests/test_evals.py`: the shipped cases load, reference only images that exist, number ≥30, cover every §1 example text, and every expected action is in the vocabulary or is `ask`; `score` accepts a list of allowed actions and checks `source`/`faces` when given; `run_eval` with a `Scripted` client (from `tests/test_router.py`) produces per-case rows and an accuracy per backend; `render_report` contains a row per case and both backends' accuracy.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Generate the fixture images through the running app or directly via ComfyUI (`/api/generate` on the dev server), convert with Pillow, commit.
- [ ] **Step 4: Run** `uv run pytest -q` — pass.
- [ ] **Step 5: Run it for real** — start the fallback by hand (not via systemd): `CUDA_VISIBLE_DEVICES= llama-server -m … --mmproj … -ngl 0 --no-mmproj-offload --host 127.0.0.1 --port 8081 -c 8192 -t 6 --jinja --no-webui`, then `uv run alchemyui router-eval --backend both`. Record accuracy and median latency for both. Tune `prompt.py` against failures *by category*, never by special-casing a fixture, and re-run. Stop the hand-started llama-server afterwards.
- [ ] **Step 6: Commit** `Phase 7: router-eval and its fixture set; results for both backends`.

---

### Task 12: Live acceptance and docs

**Files:**
- Modify: `README.md`, `docs/DECISIONS.md`, `ALCHEMYUI_SPEC.md`

- [ ] **Step 1: Live acceptance**, against the real ComfyUI, real Llamacracy (E4B) and the hand-started fallback, on a scratch database and output dir, as a test identity via forwarded headers (not the author's own library):
  1. "Generate me a photo of a spooky lighthouse at night" → a lighthouse image; chip shows the model.
  2. "Snowy Forest during Snowstorm" → generate.
  3. Example 5: "make it darker" → transform from the last image; "give me 2 variations" then "use the second one" → select; "now add a dog" → transform from t?.2.
  4. Example 3 with `person.jpg` + `bed.jpg` → an honest `ask` offering a whole-image alternative.
  5. "Me as an astronaut" with a saved likeness (the fixture portrait saved via `/api/references`) → likeness job using it.
  6. `ROUTER_PRIMARY_URL=http://127.0.0.1:9/v1` → turns plan on the fallback and the chip says "backup planner".
  7. Both LLMs down → "Snowy Forest…" still generates, chip says "planner offline".
  Record timings (router ms per backend, per action) for DECISIONS.
- [ ] **Step 2: Docs.** README: the chat UI, the router and its two backends, the `alchemyui-router` unit, `router-eval`, test count. DECISIONS: "Phase 7 — as built": the four deviations above, the mmproj finding, eval results for both backends and the chosen `ROUTER_PRIMARY_MODEL`, latencies, anything the live run taught. Spec: §7.2 gains `select`; §6 notes the stored states; §3 records that Llamacracy's models currently run without mmproj; §14 marks Phase 7 done.
- [ ] **Step 3: Run** `uv run ruff check . && uv run pytest -q` (real exit code). **Commit** `Phase 7: live acceptance notes, docs`.
