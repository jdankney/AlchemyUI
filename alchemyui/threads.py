"""Threads and turns: the chat's state machine (spec section 6).

A turn is one message and everything it caused:

    planning -> needs_input -> (answer) -> planning
             -> queued -> done | failed
    (any live state) -> cancelled

Planning runs off the GPU, concurrently, while other people's jobs hold it. A
turn waiting on its owner's answer holds no place in the queue; its jobs are
only submitted once the plan is final.

Every image a plan names is a *handle* -- a1 (this turn's attachment), t4.2
(the second image of turn 4 in this thread), me (the saved likeness) -- and
handles only ever resolve inside the requesting user's own scope. The router
is offered nothing else, and anything else is refused here regardless.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .comfy import ComfyClient
from .config import Settings
from .db import Database, now
from .faces import FaceRejected, normalise_reference
from .jobs import JobRejected, attach_mask, image_as_upload, image_public, job_public, submit_jobs
from .planner.client import LLMClient
from .planner.policy import NeedsInput, PolicyContext, compile_plan
from .planner.prompt import Attachment, thumbnail
from .planner.router import Router, RouteRequest
from .planner.schema import Plan, Vocabulary, face_handles
from .queue import TERMINAL, JobState, QueueManager
from .registry import Registry
from .search import SearchError, fetch_reference, image_search
from .storage import UploadRejected, mask_from_box, save_upload

log = logging.getLogger("alchemyui.threads")

MAX_ATTACHMENTS = 4
MAX_TEXT = 4000
SOURCE_SUFFIX = {"fallback": " · backup planner", "default": " · planner offline"}


class TurnState(enum.StrEnum):
    PLANNING = "planning"
    NEEDS_INPUT = "needs_input"
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TURN_TERMINAL = {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}


class TurnError(Exception):
    """Refused. `status` becomes the HTTP status: 404 for anything that is not
    the caller's, which is also all they are allowed to learn about it."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Scope:
    """Everything a turn may name, and nothing else."""
    attachments: dict = field(default_factory=dict)   # "a1" -> uploads row
    outputs: dict = field(default_factory=dict)       # "t4.2" -> images row
    likeness: list = field(default_factory=list)      # refs rows (is_likeness = 1)
    context: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        return [*self.attachments, *self.outputs]

    @property
    def faces(self) -> list[str]:
        return face_handles(bool(self.likeness), list(self.attachments))


# A thread row plus its cover: the newest image any of its turns made.
THREAD_SELECT = (
    "SELECT threads.*, (SELECT i.id FROM images i JOIN turns t ON i.turn_id = t.id "
    "  WHERE t.thread_id = threads.id AND i.user_id = threads.user_id "
    "  ORDER BY i.created_at DESC LIMIT 1) AS cover FROM threads ")


def build_router(settings: Settings, vocab: Vocabulary) -> Router:
    primary = fallback = None
    if settings.router_primary_url.strip() and settings.router_primary_model.strip():
        primary = LLMClient("primary", settings.router_primary_url.strip(),
                            settings.router_primary_model.strip(),
                            settings.router_primary_key.strip(),
                            settings.router_timeout_s, settings.router_primary_concurrency)
    if settings.router_fallback_url.strip():
        fallback = LLMClient("fallback", settings.router_fallback_url.strip(),
                             settings.router_fallback_model.strip() or "local", "",
                             settings.router_fallback_timeout_s,
                             settings.router_fallback_concurrency)
    return Router(vocab, primary, fallback)


def _describe_handle(handle: str) -> str:
    m = re.fullmatch(r"t(\d+)\.(\d+)", handle)
    if m:
        return f"image {m.group(2)} from message {m.group(1)}"
    if handle.startswith("a"):
        return f"attachment {handle[1:]}"
    return handle


class ChatEngine:
    def __init__(self, db: Database, qm: QueueManager, reg: Registry, settings: Settings,
                 router: Router, comfy: ComfyClient, analyser_factory=None):
        self.db, self.qm, self.reg, self.cfg = db, qm, reg, settings
        self.router, self.comfy = router, comfy
        self.vocab = Vocabulary.from_registry(reg, settings.max_batch)
        self._analyser_factory = analyser_factory
        self._tasks: dict[str, asyncio.Task] = {}
        # One lock for every state transition. A fast job must not be able to
        # settle its turn while the turn's other jobs are still being
        # submitted. Never held while calling into the queue's cancel(),
        # whose listener hook takes it too.
        self._lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------
    async def start(self) -> None:
        self.qm.add_listener(self._on_job)
        for r in await self.db.fetch_all("SELECT id FROM turns WHERE state = ?",
                                         (TurnState.PLANNING.value,)):
            log.info("re-planning turn %s after a restart", r["id"])
            self._spawn(r["id"])
        for r in await self.db.fetch_all("SELECT id FROM turns WHERE state = ?",
                                         (TurnState.QUEUED.value,)):
            await self._settle(r["id"])

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def status(self) -> dict:
        return {"router": self.router.status(), "planning": len(self._tasks)}

    # -- reading --------------------------------------------------------------
    async def _turn_row(self, user_id: int, turn_id: str):
        row = await self.db.fetch_one("SELECT * FROM turns WHERE id = ? AND user_id = ?",
                                      (turn_id, user_id))
        if row is None:
            raise TurnError("no such turn", 404)
        return row

    async def _thread_row(self, user_id: int, thread_id: str):
        row = await self.db.fetch_one(
            THREAD_SELECT + "WHERE id = ? AND user_id = ? AND deleted = 0",
            (thread_id, user_id))
        if row is None:
            raise TurnError("no such thread", 404)
        return row

    async def _row(self, turn_id: str):
        return await self.db.fetch_one("SELECT * FROM turns WHERE id = ?", (turn_id,))

    @staticmethod
    def _thread_public(r) -> dict:
        return {"id": r["id"], "title": r["title"], "created_at": r["created_at"],
                "updated_at": r["updated_at"], "model": r["model"],
                "cover_url": f"/api/images/{r['cover']}/file" if r["cover"] else None}

    async def list_threads(self, user_id: int, limit: int = 200) -> list[dict]:
        rows = await self.db.fetch_all(
            THREAD_SELECT + "WHERE user_id = ? AND deleted = 0 "
            "ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
        return [self._thread_public(r) for r in rows]

    async def get_thread(self, user_id: int, thread_id: str) -> dict:
        thread = await self._thread_row(user_id, thread_id)
        turns = await self.db.fetch_all(
            "SELECT * FROM turns WHERE thread_id = ? AND user_id = ? ORDER BY seq",
            (thread_id, user_id))
        return {"thread": self._thread_public(thread),
                "turns": [await self.turn_public(t) for t in turns]}

    async def _images_of(self, turn_id: str, user_id: int) -> list:
        return await self.db.fetch_all(
            "SELECT * FROM images WHERE turn_id = ? AND user_id = ? ORDER BY created_at, rowid",
            (turn_id, user_id))

    async def turn_public(self, row) -> dict:
        plan = json.loads(row["plan_json"]) if row["plan_json"] else None
        etas = self.qm._etas()
        jobs = []
        for r in await self.db.fetch_all(
                "SELECT * FROM jobs WHERE turn_id = ? AND user_id = ? ORDER BY created_at",
                (row["id"], row["user_id"])):
            live = self.qm._by_id.get(r["id"])
            jobs.append(live.public(self.qm.position_of(r["id"]), etas.get(r["id"]))
                        if live is not None and live.state not in TERMINAL else job_public(r))
        images = []
        for i, img in enumerate(await self._images_of(row["id"], row["user_id"])):
            images.append({**image_public(img), "handle": f"t{row['seq']}.{i + 1}"})
        question = None
        if row["question_json"] and row["state"] == TurnState.NEEDS_INPUT:
            q = json.loads(row["question_json"])
            question = {"question": q["question"],
                        "options": [o["label"] for o in q.get("options") or []],
                        "candidates": q.get("candidates") or []}
        return {
            "id": row["id"], "thread_id": row["thread_id"], "seq": row["seq"],
            "text": row["text"],
            "attachments": [{"handle": f"a{i + 1}", "id": uid, "url": f"/api/uploads/{uid}/file"}
                            for i, uid in enumerate(json.loads(row["attachments_json"]))],
            "state": row["state"],
            "pin": row["model"],
            "note": ((plan or {}).get("compiled") or {}).get("note") or None,
            "chip": plan.get("chip") if plan else None,
            "plan": plan.get("plan") if plan else None,
            "compiled": plan.get("compiled") if plan else None,
            "model": self._model_label(plan),
            "plan_source": row["plan_source"], "plan_error": row["plan_error"],
            "reply": row["reply"], "question": question,
            "answers": json.loads(row["answers_json"] or "[]"),
            "router_ms": row["router_ms"], "jobs": jobs, "images": images,
            "created_at": row["created_at"], "planned_at": row["planned_at"],
            "finished_at": row["finished_at"],
            # When this snapshot was taken. The browser gets a turn both from
            # the POST that made it and from the event stream, in either
            # order; the newer snapshot wins.
            "at": now(),
        }

    def _model_label(self, plan: dict | None) -> str | None:
        name = ((plan or {}).get("compiled") or {}).get("checkpoint")
        ck = self.reg.checkpoint(name) if name else None
        return ck.label if ck else name

    async def _emit(self, turn_id: str) -> None:
        row = await self._row(turn_id)
        thread = await self.db.fetch_one(THREAD_SELECT + "WHERE id = ?", (row["thread_id"],))
        await self.qm.emit({"type": "turn", "turn": await self.turn_public(row),
                            "thread": self._thread_public(thread)}, owner=row["user_id"])

    async def _set(self, turn_id: str, **cols) -> None:
        keys = list(cols)
        await self.db.execute(
            f"UPDATE turns SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
            [cols[k] for k in keys] + [turn_id])

    # -- writing --------------------------------------------------------------
    async def create_turn(self, user_id: int, text: str, attachment_ids: list[str],
                          thread_id: str | None = None, model: str | None = None) -> dict:
        text = (text or "").strip()
        if model and model not in {c.name for c in self.reg.switchable()}:
            raise TurnError("that model isn't one you can pick")
        model = model or None
        if not text and not attachment_ids:
            raise TurnError("type a message or attach an image")
        if len(text) > MAX_TEXT:
            raise TurnError(f"messages are limited to {MAX_TEXT} characters")
        if len(attachment_ids) > MAX_ATTACHMENTS:
            raise TurnError(f"at most {MAX_ATTACHMENTS} images per message")
        for uid in attachment_ids:
            if await self.db.fetch_one("SELECT 1 FROM uploads WHERE id = ? AND user_id = ?",
                                       (uid, user_id)) is None:
                raise TurnError("no such upload", 404)

        ts = now()
        if thread_id:
            await self._thread_row(user_id, thread_id)
        else:
            thread_id = uuid.uuid4().hex
            title = " ".join(text.split())[:60] or "Images"
            await self.db.execute(
                "INSERT INTO threads (id, user_id, title, model, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (thread_id, user_id, title, model, ts, ts))

        turn_id = uuid.uuid4().hex
        async with self._lock:
            r = await self.db.fetch_one("SELECT COALESCE(MAX(seq), 0) AS n FROM turns "
                                        "WHERE thread_id = ?", (thread_id,))
            await self.db.execute(
                "INSERT INTO turns (id, thread_id, user_id, seq, text, attachments_json, state, "
                "  model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, thread_id, user_id, r["n"] + 1, text, json.dumps(list(attachment_ids)),
                 TurnState.PLANNING.value, model, ts))
            # The thread follows its latest pick, so reopening it restores the menu.
            await self.db.execute("UPDATE threads SET updated_at = ?, model = ? WHERE id = ?",
                                  (ts, model, thread_id))
        await self._emit(turn_id)
        self._spawn(turn_id)
        return await self.turn_public(await self._row(turn_id))

    async def answer(self, user_id: int, turn_id: str, text: str | None,
                     option: int | None) -> dict:
        async with self._lock:
            row = await self._turn_row(user_id, turn_id)
            if row["state"] != TurnState.NEEDS_INPUT:
                raise TurnError("this message isn't waiting for an answer", 409)
            q = json.loads(row["question_json"] or "{}")
            options = q.get("options") or []
            forced = None
            if option is not None:
                if not 0 <= option < len(options):
                    raise TurnError("no such option")
                if options[option].get("plan"):
                    forced = Plan.from_dict(options[option]["plan"])
                reply = options[option]["label"]
            else:
                reply = (text or "").strip()
                if not reply:
                    raise TurnError("type an answer or pick an option")
            answers = json.loads(row["answers_json"] or "[]")
            answers.append({"question": q.get("question", ""), "answer": reply[:500]})
            await self._set(turn_id, state=TurnState.PLANNING.value, question_json=None,
                            reply=None, answers_json=json.dumps(answers))
        await self._emit(turn_id)
        # A forced option is still the router's plan (e.g. a reference pick).
        # Only the no-LLM default planner should be labelled "offline".
        self._spawn(turn_id, forced, row["plan_source"] or "default")
        return await self.turn_public(await self._row(turn_id))

    async def cancel(self, user_id: int, turn_id: str) -> dict:
        row = await self._turn_row(user_id, turn_id)
        if row["state"] in TURN_TERMINAL:
            raise TurnError("this message has already finished", 409)
        task = self._tasks.get(turn_id)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        live = [j for j in self.qm._by_id.values()
                if j.turn_id == turn_id and j.state not in TERMINAL]
        async with self._lock:
            await self._set(turn_id, state=TurnState.CANCELLED.value,
                            finished_at=None if live else now())
        for job in live:
            await self.qm.cancel(job.id)
        await self._emit(turn_id)
        return await self.turn_public(await self._row(turn_id))

    async def retry(self, user_id: int, turn_id: str) -> dict:
        async with self._lock:
            row = await self._turn_row(user_id, turn_id)
            plan = json.loads(row["plan_json"]) if row["plan_json"] else None
            if row["state"] not in TURN_TERMINAL or not plan or not (
                    plan.get("compiled") or {}).get("workflow"):
                raise TurnError("there is nothing to retry here", 409)
            await self._set(turn_id, state=TurnState.PLANNING.value, reply=None,
                            finished_at=None, attempt_at=now())
        await self._emit(turn_id)
        self._spawn(turn_id, Plan.from_dict(plan["plan"]), row["plan_source"])
        return await self.turn_public(await self._row(turn_id))

    async def delete_thread(self, user_id: int, thread_id: str) -> None:
        await self._thread_row(user_id, thread_id)
        for r in await self.db.fetch_all(
                "SELECT id, state FROM turns WHERE thread_id = ? AND user_id = ?",
                (thread_id, user_id)):
            if r["state"] not in TURN_TERMINAL:
                await self.cancel(user_id, r["id"])
        await self.db.execute("UPDATE threads SET deleted = 1 WHERE id = ?", (thread_id,))

    # -- scope ----------------------------------------------------------------
    async def _scope(self, row) -> Scope:
        user_id = row["user_id"]
        scope = Scope()
        for i, uid in enumerate(json.loads(row["attachments_json"])):
            up = await self.db.fetch_one("SELECT * FROM uploads WHERE id = ? AND user_id = ?",
                                         (uid, user_id))
            if up is not None:
                scope.attachments[f"a{i + 1}"] = up
        scope.likeness = list(await self.db.fetch_all(
            "SELECT * FROM refs WHERE user_id = ? AND is_likeness = 1 "
            "ORDER BY created_at LIMIT ?", (user_id, self.cfg.max_references)))

        earlier = await self.db.fetch_all(
            "SELECT * FROM turns WHERE thread_id = ? AND user_id = ? AND seq < ? ORDER BY seq",
            (row["thread_id"], user_id, row["seq"]))
        recent = earlier[-self.cfg.router_context_turns:] if self.cfg.router_context_turns else []
        recent_ids = {t["id"] for t in recent}
        focus = None
        described = {}
        for t in earlier:
            imgs = await self._images_of(t["id"], user_id)
            handles = [f"t{t['seq']}.{i + 1}" for i in range(len(imgs))]
            described[t["id"]] = handles
            if t["id"] in recent_ids:
                for h, img in zip(handles, imgs, strict=True):
                    scope.outputs[h] = img
        # What "it" means in a follow-up: the latest pick, or the latest images.
        for t in reversed(earlier):
            plan = json.loads(t["plan_json"])["plan"] if t["plan_json"] else None
            if plan and plan["action"] == "select" and t["state"] == TurnState.DONE:
                focus = f"Current image: {plan['source']} (picked by the user)."
                break
            handles = described[t["id"]]
            if len(handles) == 1:
                focus = f"Current image: {handles[0]}."
                break
            if handles:
                focus = f"Latest images: {handles[0]} to {handles[-1]} (none picked yet)."
                break
        for t in recent:
            scope.context.append(self._context_line(t, described[t["id"]]))
        if focus:
            scope.context.append(focus)
        return scope

    @staticmethod
    def _context_line(t, handles: list[str]) -> str:
        line = f'Turn {t["seq"]}: "{" ".join(t["text"].split())[:120]}"'
        n = len(json.loads(t["attachments_json"]))
        if n:
            line += f" (with {n} attachment{'s' if n > 1 else ''})"
        plan = json.loads(t["plan_json"])["plan"] if t["plan_json"] else None
        if plan:
            line += f" -> {plan['action']}"
            if plan["source"]:
                line += f" from {plan['source']}"
            if plan["action"] == "ask":
                line += f' ("{plan["question"]}")'
        if t["state"] in (TurnState.FAILED, TurnState.CANCELLED):
            line += f", which {t['state']}"
        if handles:
            line += ". Images: " + ", ".join(handles)
            if plan and plan["prompt"]:
                line += f' (prompt: "{plan["prompt"][:160]}")'
        for qa in json.loads(t["answers_json"] or "[]"):
            line += f'. Asked "{qa["question"]}", answered "{qa["answer"]}"'
        return line

    # -- planning and execution -----------------------------------------------
    def _spawn(self, turn_id: str, forced: Plan | None = None, source: str | None = None) -> None:
        task = asyncio.create_task(self._plan(turn_id, forced, source), name=f"plan-{turn_id}")
        self._tasks[turn_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(turn_id, None)
                               if self._tasks.get(turn_id) is t else None)

    async def _plan(self, turn_id: str, forced: Plan | None, forced_source: str | None) -> None:
        try:
            row = await self._row(turn_id)
            scope = await self._scope(row)
            if forced is not None:
                await self._execute(row, scope, forced, forced_source or "default")
                return
            atts = []
            for handle, up in scope.attachments.items():
                data = await asyncio.to_thread(Path(up["path"]).read_bytes)
                atts.append(Attachment(handle, await asyncio.to_thread(
                    thumbnail, data, self.cfg.router_thumb_edge)))
            req = RouteRequest(row["text"], atts, scope.context, bool(scope.likeness),
                               json.loads(row["answers_json"] or "[]"),
                               scope.sources, scope.faces)
            result = await self.router.route(req)
            if result.errors:
                log.info("turn %s planned by %s after: %s", turn_id, result.source,
                         "; ".join(result.errors))
            await self._set(
                turn_id, plan_source=result.source,
                plan_error="; ".join(result.errors)[:2000] or None,
                router_ms=(row["router_ms"] or 0) + result.ms,
                prompt_tokens=(row["prompt_tokens"] or 0) + result.prompt_tokens,
                completion_tokens=(row["completion_tokens"] or 0) + result.completion_tokens,
                planned_at=now())
            if result.plan is None:
                async with self._lock:
                    await self._park(turn_id, result.question or {"question": "?", "options": []},
                                     None)
                await self._emit(turn_id)
                return
            await self._execute(await self._row(turn_id), scope, result.plan, result.source)
        except asyncio.CancelledError:
            raise
        except (TurnError, JobRejected) as e:
            async with self._lock:
                await self._set(turn_id, state=TurnState.FAILED.value, finished_at=now(),
                                reply=str(e))
            await self._emit(turn_id)
        except Exception as e:  # noqa: BLE001 -- a turn must end somewhere visible
            log.exception("planning turn %s crashed", turn_id)
            async with self._lock:
                await self._set(turn_id, state=TurnState.FAILED.value, finished_at=now(),
                                reply=f"Something went wrong planning this ({type(e).__name__}).")
            await self._emit(turn_id)

    async def _park(self, turn_id: str, question: dict, plan: Plan | None, chip: str = "") -> None:
        """needs_input. Caller holds the lock."""
        cols = dict(state=TurnState.NEEDS_INPUT.value, question_json=json.dumps(question),
                    reply=question["question"])
        if plan is not None:
            cols["plan_json"] = json.dumps({"plan": plan.as_dict(), "compiled": None,
                                            "chip": chip or "Needs your answer"})
        await self._set(turn_id, **cols)

    async def _reference_question(self, plan: Plan) -> dict:
        """Search once, then return the candidate picker. Reused when a chosen
        image turns out to be unfetchable, so the person can pick another."""
        results = await image_search(plan.search, self.cfg)
        if not results:
            return {"question": f'I couldn\'t find any images for "{plan.search}". '
                                  "Try different words, or attach a photo instead.",
                    "options": [], "candidates": []}
        options, candidates = [], []
        for c in results:
            chosen = Plan.from_dict({**plan.as_dict(), "reference": c["url"]})
            options.append({"label": c["title"] or "Use this image",
                            "plan": chosen.as_dict()})
            candidates.append({"title": c["title"], "source": c["source"],
                               "thumb_url": c["thumb_url"]})
        return {"question": f"Which reference should I use for \"{plan.search}\"?",
                "options": options, "candidates": candidates}

    async def _execute(self, row, scope: Scope, plan: Plan, source: str) -> None:
        turn_id, user_id = row["id"], row["user_id"]
        suffix = SOURCE_SUFFIX.get(source, "")

        # Hard rule 3: whatever the grammar allowed, resolve it here, in scope.
        if plan.source and plan.source not in scope.sources:
            raise TurnError(f"the plan named {plan.source}, which is not in this conversation")
        if plan.faces and plan.faces not in scope.faces:
            raise TurnError(f"the plan named {plan.faces}, which is not a face you provided")

        if plan.action == "with_reference" and not plan.reference:
            try:
                question = await self._reference_question(plan)
            except SearchError as e:
                question = {"question": str(e), "options": [], "candidates": []}
            async with self._lock:
                await self._park(turn_id, question, plan, "Needs your pick" + suffix)
            await self._emit(turn_id)
            return

        upload, size = None, None
        if plan.source in scope.attachments:
            upload = scope.attachments[plan.source]
            size = (upload["width"], upload["height"])
        elif plan.source in scope.outputs:
            img = scope.outputs[plan.source]
            size = (img["width"], img["height"])

        ctx = PolicyContext(self.qm._resident_checkpoint, size, bool(scope.likeness), row["model"])
        try:
            compiled = compile_plan(plan, self.vocab, self.reg, ctx)
        except NeedsInput as e:
            async with self._lock:
                await self._park(turn_id, {"question": e.question, "options": e.options}, plan,
                                 "Needs your answer" + suffix)
            await self._emit(turn_id)
            return

        record = {"plan": plan.as_dict(), "compiled": compiled.as_dict(),
                  "chip": compiled.chip + suffix}
        if compiled.workflow is None:          # select: move the focus, queue nothing
            async with self._lock:
                await self._set(turn_id, state=TurnState.DONE.value, finished_at=now(),
                                plan_json=json.dumps(record),
                                reply=f"Okay, I'll work from {_describe_handle(plan.source)} next.")
            await self._emit(turn_id)
            return

        workflow = self.reg.require(compiled.workflow)
        raw = dict(compiled.raw)
        if plan.source in scope.outputs and "image" in workflow.requires:
            upload = await image_as_upload(self.db, self.comfy, self.cfg,
                                           scope.outputs[plan.source], user_id)
        if "image" in workflow.requires:
            raw["image"] = upload["comfy_name"]
        if plan.action == "with_reference" and plan.reference:
            try:
                png, _, _ = await fetch_reference(plan.reference, self.cfg)
            except SearchError as e:
                try:
                    question = await self._reference_question(plan)
                    question["question"] = f"{e} Pick another image:"
                except SearchError as again:
                    question = {"question": str(again), "options": [], "candidates": []}
                async with self._lock:
                    await self._park(turn_id, question, plan, "Needs another pick" + suffix)
                await self._emit(turn_id)
                return
            raw["garment"] = await self.comfy.upload_image(png, f"web-{uuid.uuid4().hex}.png")
        if plan.action == "edit_region" and compiled.region_mode == "box":
            if upload is None:
                raise TurnError("the plan named a source that is no longer available")
            try:
                png = mask_from_box(upload["width"], upload["height"], compiled.box)
                raw["mask"] = await attach_mask(self.db, self.comfy, self.cfg, upload, png)
            except UploadRejected as e:
                async with self._lock:
                    await self._park(turn_id, {"question": str(e), "options": []}, plan,
                                     "Needs a different area" + suffix)
                await self._emit(turn_id)
                return
        if plan.action in ("likeness", "edit_region", "with_reference") and plan.faces:
            try:
                raw["references"] = await self._faces(scope, plan.faces, user_id)
            except FaceRejected as e:
                async with self._lock:
                    await self._park(turn_id, {"question": str(e), "options": []}, plan,
                                     "Needs a clearer face" + suffix)
                await self._emit(turn_id)
                return

        async with self._lock:
            try:
                await submit_jobs(self.qm, self.db, self.cfg, workflow, raw,
                                  self.reg.checkpoint(compiled.checkpoint), user_id,
                                  count=compiled.count, upload=upload, turn_id=turn_id)
            except JobRejected as e:
                await self._set(turn_id, state=TurnState.FAILED.value, finished_at=now(),
                                plan_json=json.dumps(record), reply=str(e))
            else:
                await self._set(turn_id, state=TurnState.QUEUED.value,
                                plan_json=json.dumps(record), reply=None)
        await self._emit(turn_id)
        await self._settle(turn_id)      # a job may already have finished

    async def _faces(self, scope: Scope, handle: str, user_id: int) -> list[str]:
        if handle == "me":
            return [r["comfy_name"] for r in scope.likeness]
        # A face from this turn's attachment: validated exactly as a saved
        # likeness is, and kept for this turn only.
        up = scope.attachments[handle]
        data = await asyncio.to_thread(Path(up["path"]).read_bytes)
        analyser = (self._analyser_factory or _default_analyser)(self.cfg)
        png, meta = await asyncio.to_thread(normalise_reference, data, self.cfg, analyser,
                                            _describe_handle(handle))
        ref_id = uuid.uuid4().hex
        path = save_upload(self.cfg.upload_dir, png, f"ref-{ref_id}")
        comfy_name = await self.comfy.upload_image(png, f"ref-{ref_id}.png")
        await self.db.execute(
            "INSERT INTO refs (id, user_id, path, comfy_name, label, width, height, faces_found, "
            "  face_score, face_px, is_likeness, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)",
            (ref_id, user_id, path, comfy_name, handle, meta["width"], meta["height"],
             meta["faces_found"], meta["face_score"], meta["face_px"], now()))
        return [comfy_name]

    # -- the job hook ---------------------------------------------------------
    async def _on_job(self, job) -> None:
        if job.turn_id:
            await self._settle(job.turn_id)

    async def _settle(self, turn_id: str) -> None:
        """Once every job of a queued (or cancelled) turn is final, so is the turn."""
        async with self._lock:
            row = await self._row(turn_id)
            if row is None or row["state"] not in (TurnState.QUEUED, TurnState.CANCELLED):
                return
            if row["state"] == TurnState.CANCELLED and row["finished_at"]:
                return
            # Only this attempt's jobs: after "Run again" on a finished turn, the
            # earlier success must not hide a rerun that failed.
            jobs = await self.db.fetch_all(
                "SELECT status, error_text FROM jobs WHERE turn_id = ? AND created_at >= ? "
                "ORDER BY created_at",
                (turn_id, row["attempt_at"] or 0))
            live = {JobState.QUEUED.value, JobState.RUNNING.value}
            if not jobs or any(j["status"] in live for j in jobs):
                return
            cols: dict = {"finished_at": now()}
            if row["state"] == TurnState.QUEUED:
                if any(j["status"] == JobState.COMPLETED for j in jobs):
                    cols["state"] = TurnState.DONE.value
                elif all(j["status"] == JobState.CANCELLED for j in jobs):
                    cols["state"] = TurnState.CANCELLED.value
                else:
                    cols["state"] = TurnState.FAILED.value
                    cols["reply"] = next((j["error_text"] for j in jobs if j["error_text"]),
                                         "The generation failed.")
            await self._set(turn_id, **cols)
        await self._emit(turn_id)


def _default_analyser(settings: Settings):
    from .faces import get_analyser
    return get_analyser(settings)


_engine: ChatEngine | None = None


def get_engine() -> ChatEngine:
    if _engine is None:
        raise RuntimeError("chat engine not started")
    return _engine


async def start_engine(db, qm, reg, settings, router, comfy) -> ChatEngine:
    global _engine
    _engine = ChatEngine(db, qm, reg, settings, router, comfy)
    await _engine.start()
    return _engine


async def stop_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.stop()
        for name in ("primary", "fallback"):
            client = getattr(_engine.router, name, None)
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()
        _engine = None
