"""The queue -- the core of the project.

Strict FIFO, exactly one generation at a time, on one GPU. The app keeps its
OWN queue and submits a single prompt to ComfyUI at a time: ComfyUI's internal
queue is not the source of truth, because we need real positions, honest ETAs
and a cancel that actually aborts.

    queued -> running -> completed | failed | cancelled

Live state is pushed to every subscriber over SSE. Durations are learned per
workflow (EWMA over observed runs, seeded from the registry's `est` block), so
the ETA is measured rather than invented.

Across users the order is strictly FIFO. Checkpoint-swap-aware ordering
(_pick_next) may only reorder a user's *own* consecutive jobs at the head of
the queue: promoting a job past someone else's would be cutting in line.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import uuid
from dataclasses import dataclass, field

from .comfy import ComfyClient, ComfyError, get_comfy
from .config import Settings, get_settings
from .db import Database, get_db, now
from .meter import finalise
from .power import GpuPowerSampler
from .registry import Registry, WorkflowInfo, get_registry
from .storage import output_images, png_size, save_output

log = logging.getLogger("alchemyui.queue")


class JobState(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}


class _Everyone:
    """Viewer sentinel: sees every job. Used internally and by tests; no
    browser ever subscribes as it -- the admin page polls /api/admin/live."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "EVERYONE"


EVERYONE = _Everyone()

_RESIDENT_KEY = "resident_checkpoint"


def new_job_id() -> str:
    """Canonical lowercase hyphenated UUID -- ComfyUI validates the prompt_id
    we supply against exactly that form (server.py: validate_job_id)."""
    return str(uuid.uuid4())


@dataclass
class Job:
    workflow_id: str
    params: dict
    user_id: int | None = None     # owner; None only for v1 rows not yet claimed
    turn_id: str | None = None     # the chat turn that asked for it; None from /api/generate
    id: str = field(default_factory=new_job_id)
    state: JobState = JobState.QUEUED

    created_at: float = field(default_factory=now)
    started_at: float | None = None        # ComfyUI began executing this prompt
    sampling_started_at: float | None = None  # first sampler step -- load ends here
    finished_at: float | None = None
    picked_at: float | None = None          # worker took it: GPU occupancy starts
    occupancy_seconds: float | None = None
    gpu_watts_mean: float | None = None
    energy_wh: float | None = None
    rate_used: float | None = None
    cost_usd: float | None = None

    cold_start: bool = False
    skips: int = 0                # times this job was passed over for a swap
    progress_value: int = 0
    progress_max: int = 0
    stage_index: int = 0          # which sampling pass of a multi-stage graph
    stage_label: str = ""
    overall: float = 0.0          # 0..1 across every stage, weighted

    error_text: str | None = None
    error_detail: str | None = None

    images: list[dict] = field(default_factory=list)
    # Latest live preview frame, held in memory only: it is worthless the
    # moment the next one arrives, and writing every frame to disk would
    # churn for nothing.
    preview: bytes | None = None
    preview_media: str = "image/jpeg"
    preview_seq: int = 0

    _cancel: asyncio.Event = field(default_factory=asyncio.Event)
    _finished: asyncio.Event = field(default_factory=asyncio.Event)
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    _interrupted_upstream: bool = False

    @property
    def checkpoint(self) -> str:
        return self.params.get("checkpoint", "")

    @property
    def steps(self) -> int:
        return int(self.params.get("steps") or 0)

    def public(self, position: int | None = None, eta_s: float | None = None) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "turn_id": self.turn_id,
            "status": self.state.value,
            "position": position,
            "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "progress": {
                "value": self.progress_value, "max": self.progress_max,
                "overall": round(self.overall, 4),
                "stage": self.stage_label, "stage_index": self.stage_index,
            },
            "params": self.params,
            "cold_start": self.cold_start,
            "preview_seq": self.preview_seq,
            "error_text": self.error_text,
            "error_detail": self.error_detail,
            "images": self.images,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def meta(self, position: int | None, eta_s: float | None, owner: str) -> dict:
        """What the admin page may see: no prompt, no params, no images."""
        return {
            "id": self.id, "owner": owner, "workflow_id": self.workflow_id,
            "checkpoint": self.checkpoint, "status": self.state.value,
            "position": position, "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "overall": round(self.overall, 4), "picked_at": self.picked_at,
            "started_at": self.started_at,
        }


class QueueManager:
    def __init__(self, db: Database, comfy: ComfyClient, registry: Registry, settings: Settings,
                 sampler_factory=None):
        self.db = db
        # None means GpuPowerSampler, looked up at call time so tests can swap it.
        self._sampler_factory = sampler_factory
        self.comfy = comfy
        self.reg = registry
        self.cfg = settings

        self._pending: list[Job] = []
        self._active: Job | None = None
        self._by_id: dict[str, Job] = {}
        self._wake = asyncio.Event()
        self._subscribers: dict[asyncio.Queue, object] = {}   # queue -> viewer

        # workflow_id -> [seconds_per_step, load_seconds]
        self._stats: dict[str, list[float]] = {}
        self._resident_checkpoint: str | None = None
        self._comfy_connected = False
        self._comfy_queue_remaining = 0

        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()  # fire-and-forget, kept referenced until done
        self._listeners: list = []            # awaited with each job once it is final

    # -- lifecycle ------------------------------------------------------------
    def _stats_key(self, workflow_id: str, checkpoint: str) -> str:
        """Timings are per workflow *and* per checkpoint: SD1.5 is roughly four
        times faster than SDXL, so one average across both would be wrong for
        each of them."""
        return f"{workflow_id}|{checkpoint}"

    def _seed_for(self, workflow_id: str, checkpoint: str) -> list[float]:
        """Seed a workflow+checkpoint estimate before anything has been measured.

        The checkpoint knows its own speed. The workflow's own `est` is quoted
        against its default checkpoint, so what it contributes is a *ratio* --
        how much extra sampling it does -- not an absolute figure. A quality
        chain that costs 2.9x a plain generation costs 2.9x on either family;
        taking its SDXL seconds literally would have made SD1.5 look as slow
        as SDXL.
        """
        w = self.reg.get(workflow_id)
        c = self.reg.checkpoint(checkpoint) if checkpoint else None
        if w is None:
            return [c.est_seconds_per_step, c.est_load_s] if c else [3.0, 30.0]
        if c is None:
            return [w.est_seconds_per_step, w.est_load_s]

        base = self.reg.checkpoint(w.checkpoint)
        ratio = 1.0
        if base is not None and base.est_seconds_per_step > 0:
            ratio = max(1.0, w.est_seconds_per_step / base.est_seconds_per_step)
        return [c.est_seconds_per_step * ratio, c.est_load_s]

    async def start(self) -> None:
        for w in self.reg.all():
            for c in self.reg.checkpoints_for(w) or []:
                self._stats[self._stats_key(w.key, c.name)] = self._seed_for(w.key, c.name)
            self._stats.setdefault(self._stats_key(w.key, w.checkpoint),
                                   self._seed_for(w.key, w.checkpoint))
        for r in await self.db.fetch_all(
            "SELECT workflow_id, ewma_seconds_per_step, ewma_load_seconds FROM workflow_stats"
        ):
            self._stats[r["workflow_id"]] = [
                r["ewma_seconds_per_step"], r["ewma_load_seconds"]
            ]

        row = await self.db.fetch_one(
            "SELECT value FROM runtime_state WHERE key = ?", (_RESIDENT_KEY,)
        )
        self._resident_checkpoint = row["value"] if row else None

        await self._reconcile()

        self._tasks = [
            asyncio.create_task(self._ws_reader(), name="comfy-ws"),
            asyncio.create_task(self._worker(), name="queue-worker"),
        ]
        log.info("queue started; %d pending, resident checkpoint = %s",
                 len(self._pending), self._resident_checkpoint)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _reconcile(self) -> None:
        """Startup reconciliation (spec section 7).

        Anything left `running` is unrecoverable -- either we restarted or
        ComfyUI did, and either way that generation is gone. Mark it failed
        with a message that says so. Pending jobs, by contrast, must survive:
        they are reloaded in queue order and run as if nothing happened.
        """
        orphans = await self.db.fetch_all(
            "SELECT id FROM jobs WHERE status = ?", (JobState.RUNNING.value,)
        )
        if orphans:
            await self.db.execute(
                "UPDATE jobs SET status = ?, error_text = ?, finished_at = ? WHERE status = ?",
                (JobState.FAILED.value,
                 "The backend restarted while this job was running, so it could not be recovered.",
                 now(), JobState.RUNNING.value),
            )
            log.warning("reconciled %d orphaned running job(s) to failed", len(orphans))
            # If only the app restarted, ComfyUI is still grinding away on a
            # prompt whose result nobody will ever see. Both calls are keyed on
            # the prompt_id and no-op if it isn't there, so this cannot touch
            # anything else -- including work started from ComfyUI's own UI.
            for r in orphans:
                await self.comfy.interrupt(r["id"])
                await self.comfy.drop_pending(r["id"])

        rows = await self.db.fetch_all(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (JobState.QUEUED.value,)
        )
        for r in rows:
            if self.reg.get(r["workflow_id"]) is None:
                await self.db.execute(
                    "UPDATE jobs SET status = ?, error_text = ?, finished_at = ? WHERE id = ?",
                    (JobState.FAILED.value,
                     f"Workflow {r['workflow_id']!r} is no longer in the registry.", now(), r["id"]),
                )
                continue
            job = Job(
                workflow_id=r["workflow_id"],
                params=json.loads(r["params_json"]),
                user_id=r["user_id"],
                turn_id=r["turn_id"],
                id=r["id"],
                created_at=r["created_at"],
            )
            self._by_id[job.id] = job
            self._pending.append(job)
        if rows:
            log.info("restored %d queued job(s) across the restart", len(self._pending))
            self._wake.set()

    # -- estimates ------------------------------------------------------------
    def estimate(self, workflow_id: str, steps: int, cold: bool, checkpoint: str = "") -> float:
        key = self._stats_key(workflow_id, checkpoint)
        sps, load = self._stats.get(key) or self._seed_for(workflow_id, checkpoint)
        return steps * sps + (load if cold else 0.0)

    def _job_estimate(self, job: Job, assume_resident: str | None) -> float:
        return self.estimate(job.workflow_id, job.steps,
                             job.checkpoint != assume_resident, job.checkpoint)

    def _remaining_for_active(self) -> float:
        """Seconds left on the running job: prefer the real step count, fall
        back to the estimate before the first step lands."""
        job = self._active
        if job is None:
            return 0.0
        # Measured beats modelled: once a job is far enough in, extrapolate
        # from its own elapsed time rather than from a per-step average that
        # knows nothing about which stage is running.
        if job.overall > 0.02 and job.sampling_started_at:
            elapsed = now() - job.sampling_started_at
            return max(0.0, elapsed / job.overall - elapsed)
        key = self._stats_key(job.workflow_id, job.checkpoint)
        sps = (self._stats.get(key) or self._seed_for(job.workflow_id, job.checkpoint))[0]
        if job.progress_max and job.progress_value:
            return max(0.0, (job.progress_max - job.progress_value) * sps)
        return self._job_estimate(job, self._resident_checkpoint)

    def _etas(self) -> dict[str, float]:
        """ETA per job id: the running job's remaining time, then each pending
        job's own estimate stacked on top, accounting for swaps along the way."""
        etas: dict[str, float] = {}
        acc = self._remaining_for_active()
        if self._active is not None:
            etas[self._active.id] = acc
        resident = self._active.checkpoint if self._active else self._resident_checkpoint
        for j in self._pending:
            acc += self._job_estimate(j, resident)
            etas[j.id] = acc
            resident = j.checkpoint
        return etas

    def _observe(self, job: Job) -> None:
        """Fold a completed run into the per-workflow EWMAs.

        Load and sampling are learned separately because they scale with
        completely different things: a checkpoint swap costs 30s+ regardless of
        step count, while sampling is linear in steps. The first sampler step
        is the boundary between the two.
        """
        if not (job.started_at and job.finished_at and job.steps):
            return
        key = self._stats_key(job.workflow_id, job.checkpoint)
        st = self._stats.setdefault(key, self._seed_for(job.workflow_id, job.checkpoint))
        a = self.cfg.ewma_alpha

        sampling_from = job.sampling_started_at or job.started_at
        # Sampling through save: an ETA should cover the whole job, not just
        # the denoise loop, so VAE decode and the write are folded in here.
        per_step = (job.finished_at - sampling_from) / job.steps
        if per_step > 0:
            st[0] = (1 - a) * st[0] + a * per_step

        if job.cold_start and job.sampling_started_at:
            observed_load = job.sampling_started_at - job.started_at
            if observed_load > 0:
                st[1] = (1 - a) * st[1] + a * observed_load

        t = asyncio.create_task(self._persist_stats(key, st))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _persist_stats(self, workflow_id: str, st: list[float]) -> None:
        await self.db.execute(
            "INSERT INTO workflow_stats "
            "  (workflow_id, ewma_seconds_per_step, ewma_load_seconds, samples, updated_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(workflow_id) DO UPDATE SET "
            "  ewma_seconds_per_step = excluded.ewma_seconds_per_step, "
            "  ewma_load_seconds = excluded.ewma_load_seconds, "
            "  samples = workflow_stats.samples + 1, "
            "  updated_at = excluded.updated_at",
            (workflow_id, st[0], st[1], now()),
        )

    async def _set_resident(self, checkpoint: str | None) -> None:
        """ComfyUI has no /running endpoint, so what is resident is tracked
        from the last job we saw complete and persisted so a restart does not
        start lying. It is only ever shown as a hint."""
        if checkpoint == self._resident_checkpoint:
            return
        self._resident_checkpoint = checkpoint
        await self.db.execute(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_RESIDENT_KEY, checkpoint or ""),
        )

    # -- submission / cancellation --------------------------------------------
    async def submit(self, workflow: WorkflowInfo, params: dict,
                     user_id: int | None = None, turn_id: str | None = None) -> Job:
        job = Job(workflow_id=workflow.key, params=params, user_id=user_id, turn_id=turn_id)
        self._by_id[job.id] = job
        self._pending.append(job)
        await self._persist_job(job)
        self._wake.set()
        await self._broadcast()
        log.info("job %s queued (workflow=%s user=%s pos=%d)",
                 job.id, job.workflow_id, user_id, len(self._pending))
        return job

    async def cancel(self, job_id: str, user_id: int | None = None) -> bool:
        """Cancel a job. With `user_id`, only its owner may -- anyone else is
        told it does not exist, which is also all they are allowed to learn."""
        job = self._by_id.get(job_id)
        if job is None or job.state in TERMINAL:
            return False
        if user_id is not None and job.user_id != user_id:
            return False

        if job is self._active:
            job._cancel.set()
            # Targeted: ComfyUI no-ops if this prompt is not the running one,
            # so we can never kill something started from its own UI.
            await self.comfy.interrupt(job.id)
            return True

        if job in self._pending:
            self._pending.remove(job)
            job.state = JobState.CANCELLED
            job.finished_at = now()
            await self._persist_job(job)
            job._done.set()
            await self._broadcast()
            await self._emit({"type": "job", "job": job.public()}, owner=job.user_id)
            await self._notify(job)
            return True
        return False

    # -- listeners ------------------------------------------------------------
    def add_listener(self, callback) -> None:
        """`await callback(job)` once each job is final -- completed, failed,
        or cancelled whether queued or running. The turn engine moves turns
        along from here."""
        self._listeners.append(callback)

    async def _notify(self, job: Job) -> None:
        for cb in list(self._listeners):
            try:
                await cb(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a listener bug must never stop the worker
                log.exception("job listener failed for %s", job.id)

    async def cancel_user(self, user_id: int) -> int:
        """Cancel everything a user has queued or running (a disabled account)."""
        mine = [j for j in [self._active, *self._pending] if j and j.user_id == user_id]
        n = 0
        for job in mine:
            n += await self.cancel(job.id)
        return n

    # -- snapshot / SSE fan-out -----------------------------------------------
    @staticmethod
    def _visible(job: Job, viewer: object) -> bool:
        return viewer is EVERYONE or job.user_id == viewer

    def snapshot(self, viewer: object = EVERYONE) -> dict:
        """The queue as one viewer may see it: their own jobs in full, with
        positions counted across everyone ("2 ahead of you" stays true), and
        nothing at all about anyone else's beyond the depth."""
        etas = self._etas()
        jobs = []
        ahead = 0
        if self._active is not None:
            if self._visible(self._active, viewer):
                jobs.append(self._active.public(0, etas.get(self._active.id)))
            ahead = 1
        for i, j in enumerate(self._pending):
            if self._visible(j, viewer):
                jobs.append(j.public(i + ahead, etas.get(j.id)))
        return {
            "type": "queue",
            "jobs": jobs,
            "depth": len(self._pending) + (1 if self._active else 0),
            "resident_checkpoint": self._resident_checkpoint,
            "comfy_connected": self._comfy_connected,
            "ts": now(),
        }

    def admin_view(self, names: dict[int, str]) -> dict:
        etas = self._etas()
        rows, ahead = [], 0
        if self._active is not None:
            a = self._active
            rows.append(a.meta(0, etas.get(a.id), names.get(a.user_id, "unclaimed")))
            ahead = 1
        for i, j in enumerate(self._pending):
            rows.append(j.meta(i + ahead, etas.get(j.id), names.get(j.user_id, "unclaimed")))
        return {"jobs": rows, "depth": len(rows)}

    def position_of(self, job_id: str) -> int | None:
        """How many jobs are ahead of this one. 0 means it is running, or is
        about to be because nothing else holds the GPU.

        Counting jobs-ahead rather than place-in-line is what lets the UI say
        "2 ahead of you" literally. The first job into an empty queue is 0,
        not 1: there is nobody in front of it.
        """
        if self._active and self._active.id == job_id:
            return 0
        ahead = 1 if self._active else 0
        for i, j in enumerate(self._pending):
            if j.id == job_id:
                return i + ahead
        return None

    def subscribe(self, viewer: object = EVERYONE) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[q] = viewer
        q.put_nowait(self.snapshot(viewer))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def _put(self, q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # A subscriber this far behind is a dead connection; the
            # browser will reconnect and recover state from /api/jobs.
            self._subscribers.pop(q, None)

    async def _emit(self, event: dict, owner: object = EVERYONE) -> None:
        """Send to everyone entitled to it: a job's events go to its owner only."""
        for q, viewer in list(self._subscribers.items()):
            if owner is EVERYONE or viewer is EVERYONE or viewer == owner:
                self._put(q, event)

    async def emit(self, event: dict, owner: object = EVERYONE) -> None:
        """Put an event on the one SSE stream, for its owner only. Other parts
        of the app (the turn engine) share the stream rather than open another."""
        await self._emit(event, owner)

    async def _broadcast(self) -> None:
        snaps: dict[object, dict] = {}
        for q, viewer in list(self._subscribers.items()):
            if viewer not in snaps:
                snaps[viewer] = self.snapshot(viewer)
            self._put(q, snaps[viewer])

    # -- worker ---------------------------------------------------------------
    def _pick_next(self) -> Job:
        """Choose the next job to run, preferring one that needs no model swap.

        A checkpoint swap costs 30s+ -- a quarter of a plain SDXL generation,
        and more than a whole SD1.5 one. So within a bounded look-ahead, a job
        whose checkpoint is already resident is promoted ahead of one that
        would force a swap.

        Two things keep that honest. The window is capped, so a job can only
        ever be overtaken by jobs near the front. And every job passed over
        counts a skip; once a job has been skipped `queue_max_skips` times it
        goes next regardless of what it costs. Strict FIFO is the fallback,
        not the exception -- with one checkpoint in play this does nothing at
        all, which is exactly what Phase 1 wanted.

        v2: all of that happens only inside the contiguous run of jobs that
        belong to the owner at the head. Across users the queue is strict
        FIFO, and a skip counter can never pull someone's job out from behind
        another user's either.
        """
        head = self._pending[0]
        run: list[Job] = []
        for job in self._pending:
            if job.user_id != head.user_id:
                break
            run.append(job)

        # Anyone in this run who has waited too long goes now, swap or no swap.
        for job in run:
            if job.skips >= self.cfg.queue_max_skips:
                self._pending.remove(job)
                return job

        resident = self._resident_checkpoint
        if resident is None or head.checkpoint == resident:
            return self._pending.pop(0)

        window = run[:max(1, self.cfg.queue_reorder_window)]
        for i, job in enumerate(window):
            if job.checkpoint == resident:
                if i == 0:
                    return self._pending.pop(0)
                for passed in window[:i]:
                    passed.skips += 1
                self._pending.remove(job)
                log.info("job %s promoted over %d of its owner's job(s): %s is already resident",
                         job.id, i, job.checkpoint)
                return job
        return self._pending.pop(0)

    async def _worker(self) -> None:
        while True:
            while not self._pending:
                self._wake.clear()
                await self._wake.wait()
            job = self._pick_next()
            if job.state in TERMINAL:
                continue

            self._active = job
            job.picked_at = now()
            sampler = (self._sampler_factory or GpuPowerSampler)()
            sampler.start()
            job.state = JobState.RUNNING
            job.cold_start = job.checkpoint != self._resident_checkpoint
            await self._persist_job(job)
            await self._broadcast()
            await self._emit({"type": "job", "job": job.public(0)}, owner=job.user_id)

            shutting_down = False
            try:
                await self._run(job)
            except ComfyError as e:
                job.state = JobState.FAILED
                job.error_text = e.message
                job.error_detail = e.detail
            except asyncio.CancelledError:
                # The app is going down with this job in flight. Deliberately
                # leave the row `running` and finalise nothing: the generation
                # did not survive, and _reconcile() on the next boot is the
                # only thing that knows that. Claiming success here would
                # record an image that never existed.
                shutting_down = True
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("job %s crashed", job.id)
                job.state = JobState.FAILED
                job.error_text = "The app hit an unexpected error handling this job."
                job.error_detail = repr(e)
            finally:
                await sampler.stop()
                self._active = None
                if not shutting_down:
                    if job.finished_at is None:
                        job.finished_at = now()
                    if job.state not in TERMINAL:
                        job.state = JobState.COMPLETED
                    # Everything that held the GPU is metered, failures and
                    # cancellations too, and the result is frozen on the row.
                    r = finalise(job.picked_at, job.finished_at, sampler.mean, self.cfg)
                    job.occupancy_seconds, job.gpu_watts_mean = r.occupancy_seconds, r.gpu_watts_mean
                    job.energy_wh, job.rate_used, job.cost_usd = r.energy_wh, r.rate_used, r.cost_usd
                    await self._persist_job(job)
                    job.preview = None          # the real image supersedes it
                    job._done.set()
                    await self._emit({"type": "job", "job": job.public()}, owner=job.user_id)
                    await self._broadcast()
                    await self._notify(job)

    async def _run(self, job: Job) -> None:
        workflow = self.reg.require(job.workflow_id)
        graph = workflow.build_graph(job.params)

        # We mint the prompt_id, so the WS relay can key on it from this
        # moment -- no race against the POST response coming back.
        await self.comfy.submit(graph, job.id)

        budget = self.cfg.job_start_timeout_s + 10 * max(
            1.0, self._job_estimate(job, self._resident_checkpoint)
        )
        finished = asyncio.create_task(job._finished.wait())
        cancelled = asyncio.create_task(job._cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {finished, cancelled}, timeout=budget, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                job.state = JobState.FAILED
                job.error_text = (
                    f"ComfyUI did not finish this job within {round(budget)}s. "
                    "It may be stuck, or busy with work submitted from its own interface."
                )
                await self.comfy.interrupt(job.id)
                await self.comfy.drop_pending(job.id)
                return

            if cancelled in done and not job._finished.is_set():
                # /interrupt already went out from cancel(); give ComfyUI a
                # moment to unwind and confirm before we call it cancelled.
                await self.comfy.drop_pending(job.id)
                try:
                    await asyncio.wait_for(job._finished.wait(), timeout=30.0)
                except TimeoutError:
                    log.warning("job %s: no interrupt confirmation from ComfyUI", job.id)
        finally:
            finished.cancel()
            cancelled.cancel()

        job.finished_at = now()

        if job._cancel.is_set() or job._interrupted_upstream:
            job.state = JobState.CANCELLED
            # A partial run still leaves the checkpoint resident.
            await self._set_resident(job.checkpoint)
            return

        if job.error_text:
            job.state = JobState.FAILED
            await self._set_resident(job.checkpoint)
            return

        # A run can still fail at this point (no image came back), so _collect
        # reports rather than the caller assuming success.
        if await self._collect(job, workflow):
            job.state = JobState.COMPLETED
            self._observe(job)
        else:
            job.state = JobState.FAILED
        await self._set_resident(job.checkpoint)

    async def _collect(self, job: Job, workflow: WorkflowInfo) -> bool:
        """Read /history once, copy the images into app-owned storage.

        Returns False if the run produced nothing to show.

        This is the only /history call for a job: while the WebSocket is
        connected it carries everything, and polling would just add load to a
        machine that is busy with a GPU.
        """
        history = await self.comfy.history(job.id)
        descriptors = output_images(history, workflow.output_node)
        if not descriptors:
            job.error_text = "The workflow finished but produced no image."
            job.error_detail = json.dumps(history, indent=2)[:8000]
            return False

        params_json = json.dumps(job.params)
        for d in descriptors:
            data = await self.comfy.view(
                d.get("filename", ""), d.get("subfolder", ""), d.get("type", "output")
            )
            image_id, path = save_output(
                self.cfg.output_dir, job.id, d.get("filename", "image.png"), data
            )
            width, height = png_size(data)
            ts = now()
            await self.db.execute(
                "INSERT INTO images (id, user_id, job_id, filename, path, width, height, seed, "
                "  workflow_id, params_json, favorite, turn_id, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (image_id, job.user_id, job.id, d.get("filename", ""), path, width, height,
                 job.params.get("seed"), job.workflow_id, params_json, job.turn_id, ts),
            )
            row = {
                "id": image_id, "job_id": job.id, "width": width, "height": height,
                "seed": job.params.get("seed"), "workflow_id": job.workflow_id,
                "params": job.params, "favorite": False, "created_at": ts,
                "turn_id": job.turn_id,
                "url": f"/api/images/{image_id}/file",
            }
            job.images.append(row)
            await self._emit({"type": "image", "image": row}, owner=job.user_id)
        return True

    # -- ComfyUI WebSocket relay ----------------------------------------------
    async def _ws_reader(self) -> None:
        """One socket for the app's lifetime; frames dispatched by prompt_id."""
        async for msg in self.comfy.watch():
            try:
                await self._handle_ws(msg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a bad frame must not kill the relay
                log.exception("failed handling comfy ws message %r", msg.get("type"))

    async def _handle_ws(self, msg: dict) -> None:
        t = msg.get("type")
        d = msg.get("data") or {}

        if t == "_ws_connected":
            self._comfy_connected = True
            await self._broadcast()
            return
        if t == "_ws_disconnected":
            self._comfy_connected = False
            await self._broadcast()
            return
        if t == "_preview":
            # Frames carry no prompt_id, but only one job ever holds the GPU,
            # so the active one is unambiguous.
            job = self._active
            if job is not None:
                job.preview = d["bytes"]
                job.preview_media = d["media_type"]
                job.preview_seq += 1
                # The frame itself is fetched over HTTP; SSE only says a newer
                # one exists, so a 50KB image never rides the event stream.
                await self._emit({"type": "preview", "job_id": job.id,
                                  "seq": job.preview_seq}, owner=job.user_id)
            return
        if t == "status":
            info = ((d.get("status") or {}).get("exec_info") or {})
            self._comfy_queue_remaining = info.get("queue_remaining", 0)
            return

        job = self._by_id.get(d.get("prompt_id") or "")
        if job is None:
            return

        if t == "execution_start":
            job.started_at = now()
            await self._persist_job(job)
            await self._broadcast()

        elif t in ("progress_state", "progress"):
            if self._apply_progress(job, t, d):
                await self._emit({
                    "type": "progress",
                    "job_id": job.id,
                    "value": job.progress_value,
                    "max": job.progress_max,
                    "overall": round(job.overall, 4),
                    "stage": job.stage_label,
                    "stage_index": job.stage_index,
                    "eta_s": round(self._remaining_for_active(), 1),
                    "started_at": job.started_at,
                }, owner=job.user_id)

        elif t == "executing":
            # node == null means this prompt is finished.
            if d.get("node") is None:
                job._finished.set()

        elif t == "execution_success":
            job._finished.set()

        elif t == "execution_interrupted":
            job._interrupted_upstream = True
            job._finished.set()

        elif t == "execution_error":
            node = d.get("node_type") or d.get("node_id")
            message = d.get("exception_message") or "ComfyUI raised an error"
            job.error_text = f"{node}: {message}" if node else message
            # The traceback is kept verbatim: on a GTX 16-series card, black or
            # NaN output is a precision problem, not an app bug, and only the
            # real trace tells the two apart.
            job.error_detail = json.dumps(d, indent=2)[:8000]
            log.error("execution_error for job %s: %s", job.id, job.error_detail)
            job._finished.set()

    def _apply_progress(self, job: Job, kind: str, d: dict) -> bool:
        """Update a job's progress. Returns True if anything changed.

        ComfyUI 0.30 sends `progress_state` -- a map of every active node's
        progress -- and no longer sends the flat `progress` message the spec
        describes. We read the workflow's declared sampling stages out of that
        map, and keep a legacy branch so an older ComfyUI still drives the bar.
        """
        workflow = self.reg.get(job.workflow_id)
        stages = workflow.stages if workflow else ()

        if kind == "progress":
            value, max_ = d.get("value"), d.get("max")
            index = job.stage_index
        else:
            nodes = d.get("nodes") or {}
            value = max_ = None
            index = job.stage_index
            # Prefer the latest stage that is reporting: a graph moves forward,
            # and a node that already finished may still sit in the map.
            for i, st in enumerate(stages):
                entry = nodes.get(st.node)
                if entry is not None and entry.get("max"):
                    value, max_, index = entry.get("value"), entry.get("max"), i
            if value is None:
                # A stage that reports nothing (FaceDetailer does not always),
                # or a template whose real work happens off the declared path:
                # follow whichever node has the most steps.
                candidates = [n for n in nodes.values() if (n.get("max") or 0) > 1]
                best = max(candidates, key=lambda n: n.get("max") or 0, default=None)
                if best is not None:
                    value, max_ = best.get("value"), best.get("max")

        if value is None or max_ is None:
            return False
        value, max_ = int(value), int(max_)

        # ComfyUI reports a 0/1 marker for any node that is merely *running*
        # -- the checkpoint loader, the text encoders, and the sampler itself
        # before it starts stepping. Only a sampler has real steps, so a max
        # of 1 on a job asking for more than that is a marker, not progress.
        if max_ <= 1 and job.steps > 1:
            return False

        overall = self._overall(stages, index, value, max_)
        label = stages[index].label if index < len(stages) else ""
        if (value, max_, index, round(overall, 4)) == (
                job.progress_value, job.progress_max, job.stage_index, round(job.overall, 4)):
            return False

        job.progress_value, job.progress_max = value, max_
        job.stage_index, job.stage_label = index, label
        # Never let the bar go backwards when a new stage starts from step 1.
        job.overall = max(job.overall, overall)

        if value >= 1 and job.sampling_started_at is None:
            # First real step: everything before this was checkpoint load and
            # text encode, which is exactly the split the EWMA wants.
            job.sampling_started_at = now()
        return True

    @staticmethod
    def _overall(stages, index: int, value: int, max_: int) -> float:
        """Weighted completion across every stage.

        Reaching stage N means every earlier stage is done, whether or not it
        ever reported -- which is what keeps a silent stage from stalling the
        bar at the previous stage's total.
        """
        if not stages:
            return value / max_ if max_ else 0.0
        total = sum(st.weight for st in stages)
        done = sum(st.weight for st in stages[:index])
        here = stages[index].weight * (value / max_ if max_ else 0.0)
        return min(1.0, (done + here) / total)

    # -- persistence ----------------------------------------------------------
    async def _persist_job(self, job: Job) -> None:
        await self.db.execute(
            "INSERT INTO jobs (id, workflow_id, user_id, status, prompt_id, queue_position, "
            "  params_json, input_image_path, mask_path, error_text, error_detail, checkpoint, "
            "  steps, created_at, started_at, finished_at, picked_at, occupancy_seconds, "
            "  gpu_watts_mean, energy_wh, rate_used, cost_usd, turn_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  status=excluded.status, queue_position=excluded.queue_position, "
            "  error_text=excluded.error_text, error_detail=excluded.error_detail, "
            "  started_at=excluded.started_at, finished_at=excluded.finished_at, "
            "  picked_at=excluded.picked_at, occupancy_seconds=excluded.occupancy_seconds, "
            "  gpu_watts_mean=excluded.gpu_watts_mean, energy_wh=excluded.energy_wh, "
            "  rate_used=excluded.rate_used, cost_usd=excluded.cost_usd",
            (job.id, job.workflow_id, job.user_id, job.state.value, job.id,
             self.position_of(job.id),
             json.dumps(job.params), None, None, job.error_text, job.error_detail,
             job.checkpoint, job.steps, job.created_at, job.started_at, job.finished_at,
             job.picked_at, job.occupancy_seconds, job.gpu_watts_mean, job.energy_wh,
             job.rate_used, job.cost_usd, job.turn_id),
        )


_qm: QueueManager | None = None


def get_queue() -> QueueManager:
    if _qm is None:
        raise RuntimeError("queue not started")
    return _qm


async def start_queue() -> QueueManager:
    global _qm
    _qm = QueueManager(get_db(), get_comfy(), get_registry(), get_settings())
    await _qm.start()
    return _qm


async def stop_queue() -> None:
    global _qm
    if _qm is not None:
        await _qm.stop()
        _qm = None
