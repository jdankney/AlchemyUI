"""The queue: strict FIFO, one job at a time, a cancel that really aborts,
durations that are learned rather than invented, and a restart that loses
nothing it shouldn't.

No GPU and no ComfyUI -- FakeComfy emits the same WebSocket script a real 0.30
server does.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from alchemyui.config import Settings
from alchemyui.db import Database
from alchemyui.queue import Job, JobState, QueueManager
from alchemyui.registry import load_registry
from tests.conftest import FakeComfy

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / "workflows"


def _settings(output_dir: str) -> Settings:
    s = Settings()
    object.__setattr__(s, "output_dir", output_dir)
    object.__setattr__(s, "job_start_timeout_s", 5.0)
    return s


async def _make_qm(db_path: str, output_dir: str, comfy: FakeComfy) -> QueueManager:
    qm = QueueManager(Database(db_path), comfy, load_registry(WORKFLOWS), _settings(output_dir))
    await qm.start()
    return qm


async def _submit(qm: QueueManager, **over):
    w = qm.reg.require("txt2img-sdxl")
    params = w.resolve({"prompt": "a cat", "steps": 4, **over})
    return await qm.submit(w, params)


# --- FIFO -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fifo_one_at_a_time(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.delay = 0.03
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        a = await _submit(qm)
        b = await _submit(qm)
        await asyncio.sleep(0.02)
        assert qm._active is a
        assert qm.position_of(b.id) == 1          # a real position, not a spinner

        await asyncio.wait_for(a._done.wait(), 10)
        await asyncio.wait_for(b._done.wait(), 10)

        assert [pid for pid, _ in comfy.submitted] == [a.id, b.id]
        rows = {r["id"]: r for r in await qm.db.fetch_all("SELECT * FROM jobs")}
        assert rows[a.id]["status"] == "completed"
        # b only started once a was finished -- never two prompts in flight
        assert rows[b.id]["started_at"] >= rows[a.id]["finished_at"]
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_we_supply_the_prompt_id_and_it_is_a_canonical_uuid(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        pid, _ = comfy.submitted[0]
        assert pid == job.id
        assert len(pid) == 36 and pid.count("-") == 4 and pid == pid.lower()
    finally:
        await qm.stop()


# --- the wire format --------------------------------------------------------
@pytest.mark.asyncio
async def test_progress_state_drives_the_step_counter(tmp_db_path, tmp_output_dir):
    """ComfyUI 0.30 sends `progress_state`, not the flat `progress` the spec
    describes. If this regresses, the progress bar silently never moves."""
    comfy = FakeComfy()
    comfy.steps = 6
    comfy.delay = 0.02
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        seen = []
        job = await _submit(qm, steps=6)
        for _ in range(60):
            await asyncio.sleep(0.01)
            if job.progress_max:
                seen.append((job.progress_value, job.progress_max))
            if job._done.is_set():
                break
        await asyncio.wait_for(job._done.wait(), 10)
        assert seen, "no progress was ever reported"
        assert max(v for v, _ in seen) == 6
        assert all(m == 6 for _, m in seen)
        assert job.sampling_started_at is not None   # first step marks load->sample
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_legacy_progress_message_still_works(tmp_db_path, tmp_output_dir):
    """Older ComfyUI builds send the flat message; keep driving the bar."""
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        assert qm._apply_progress(job, "progress", {"value": 3, "max": 10}) is True
        assert (job.progress_value, job.progress_max) == (3, 10)
        assert qm._apply_progress(job, "progress", {"value": 3, "max": 10}) is False  # no churn
        await asyncio.wait_for(job._done.wait(), 10)
    finally:
        await qm.stop()


# --- cancellation -----------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_while_queued_never_runs(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.delay = 0.05
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        a = await _submit(qm)
        b = await _submit(qm)
        await asyncio.sleep(0.01)
        assert await qm.cancel(b.id) is True
        await asyncio.wait_for(a._done.wait(), 10)

        assert b.state is JobState.CANCELLED
        assert b.id not in [pid for pid, _ in comfy.submitted]   # never reached ComfyUI
        row = await qm.db.fetch_one("SELECT status FROM jobs WHERE id=?", (b.id,))
        assert row["status"] == "cancelled"
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cancel_while_running_really_interrupts(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.steps = 30
    comfy.delay = 0.02
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm, steps=30)
        await asyncio.sleep(0.1)
        assert await qm.cancel(job.id) is True
        await asyncio.wait_for(job._done.wait(), 10)

        assert comfy.interrupts == [job.id]        # targeted, by prompt_id
        assert job.state is JobState.CANCELLED
        assert job.progress_value < 30             # stopped part-way
        row = await qm.db.fetch_one("SELECT status FROM jobs WHERE id=?", (job.id,))
        assert row["status"] == "cancelled"
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cancelling_a_finished_job_is_a_no_op(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        assert await qm.cancel(job.id) is False
        assert await qm.cancel("nope") is False
    finally:
        await qm.stop()


# --- failure ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_execution_error_keeps_the_traceback(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.mode = "error"
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        assert job.state is JobState.FAILED
        assert "KSampler" in job.error_text
        assert "same device" in job.error_text
        assert "RuntimeError" in job.error_detail      # the full payload survives
        row = await qm.db.fetch_one("SELECT status, error_detail FROM jobs WHERE id=?", (job.id,))
        assert row["status"] == "failed" and "traceback" in row["error_detail"]
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_rejected_prompt_fails_the_job_and_frees_the_queue(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.mode = "reject"
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        bad = await _submit(qm)
        await asyncio.wait_for(bad._done.wait(), 10)
        assert bad.state is JobState.FAILED
        assert "sampler_name" in bad.error_text        # node_errors -> readable line

        comfy.mode = "ok"                              # the worker keeps going
        good = await _submit(qm)
        await asyncio.wait_for(good._done.wait(), 10)
        assert good.state is JobState.COMPLETED
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_run_that_produces_no_image_is_a_failure(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.images = []
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        assert job.state is JobState.FAILED
        assert "no image" in job.error_text
    finally:
        await qm.stop()


# --- outputs ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_images_are_copied_into_app_storage_with_full_params(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm, seed=1234)
        await asyncio.wait_for(job._done.wait(), 10)

        rows = await qm.db.fetch_all("SELECT * FROM images WHERE job_id=?", (job.id,))
        assert len(rows) == 1
        r = rows[0]
        assert r["seed"] == 1234
        assert (r["width"], r["height"]) == (1, 1)      # read from the real PNG header
        # the file is ours, under OUTPUT_DIR -- not a pointer into ComfyUI
        assert Path(r["path"]).exists()
        assert Path(r["path"]).is_relative_to(Path(tmp_output_dir))
        # the full parameter set rides along: reuse, vary-seed, reproducibility
        params = json.loads(r["params_json"])
        assert params["prompt"] == "a cat"
        assert {"steps", "cfg", "sampler", "scheduler", "checkpoint", "width"} <= set(params)
    finally:
        await qm.stop()


# --- learned estimates ------------------------------------------------------
@pytest.mark.asyncio
async def test_durations_are_learned_and_persisted(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.delay = 0.02
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        # Timings are keyed per workflow *and* checkpoint: SD1.5 is roughly
        # four times faster than SDXL, so one average across both is wrong for
        # each of them.
        key = qm._stats_key("txt2img-sdxl", qm.reg.require("txt2img-sdxl").checkpoint)
        seeded = qm._stats[key][0]
        assert seeded == qm._seed_for("txt2img-sdxl", qm.reg.require("txt2img-sdxl").checkpoint)[0]

        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        await asyncio.sleep(0.05)                  # the persist is fire-and-forget

        assert qm._stats[key][0] != seeded             # moved toward what we measured
        row = await qm.db.fetch_one(
            "SELECT samples FROM workflow_stats WHERE workflow_id = ?", (key,))
        assert row and row["samples"] >= 1
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_eta_stacks_across_the_queue(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.delay = 0.05
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        await _submit(qm)
        b = await _submit(qm)
        c = await _submit(qm)
        await asyncio.sleep(0.02)
        etas = qm._etas()
        # the job further back waits for everything in front of it
        assert etas[c.id] > etas[b.id] > 0
        snap = qm.snapshot()
        assert [j["position"] for j in snap["jobs"]] == [0, 1, 2]
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_resident_checkpoint_is_tracked_and_survives_restart(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        assert qm._resident_checkpoint is None
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        assert job.cold_start is True                       # nothing was loaded
        assert qm._resident_checkpoint == job.checkpoint
    finally:
        await qm.stop()

    qm2 = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        assert qm2._resident_checkpoint == "RealVisXL_V5.0_fp16.safetensors"
        warm = await _submit(qm2)
        await asyncio.wait_for(warm._done.wait(), 10)
        assert warm.cold_start is False                     # no swap to pay for
    finally:
        await qm2.stop()


# --- restart ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_restart_keeps_pending_jobs_and_fails_orphaned_running_ones(
    tmp_db_path, tmp_output_dir
):
    comfy = FakeComfy()
    comfy.mode = "hang"            # accepted by ComfyUI, never finishes
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    running = await _submit(qm)
    queued = await _submit(qm)
    await asyncio.sleep(0.05)
    assert qm._active is running
    await qm.stop()                # simulate the backend going away mid-job

    qm2 = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        orphan = await qm2.db.fetch_one(
            "SELECT status, error_text FROM jobs WHERE id=?", (running.id,))
        assert orphan["status"] == "failed"
        assert "restarted" in orphan["error_text"]
        # and ComfyUI is told to stop working on a result nobody will see
        assert running.id in qm2.comfy.interrupts
        assert running.id in qm2.comfy.drops

        # the pending job, by contrast, is picked up and run as if nothing happened
        restored = qm2._by_id.get(queued.id) or next(
            (j for j in qm2._pending if j.id == queued.id), None)
        assert restored is not None
        await asyncio.wait_for(restored._done.wait(), 10)
        assert restored.state is JobState.COMPLETED
    finally:
        await qm2.stop()


# --- SSE fan-out ------------------------------------------------------------
@pytest.mark.asyncio
async def test_subscribers_get_a_snapshot_then_live_events(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        q = qm.subscribe()
        first = await asyncio.wait_for(q.get(), 1)
        assert first["type"] == "queue"          # a snapshot on connect

        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 10)
        await asyncio.sleep(0.05)

        types = set()
        while not q.empty():
            types.add(q.get_nowait()["type"])
        assert {"queue", "job", "progress", "image"} <= types
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_node_running_markers_are_not_mistaken_for_steps(tmp_db_path, tmp_output_dir):
    """Before the sampler starts stepping, ComfyUI reports 0/1 for whichever
    node is merely running. Treating that as progress flashes "step 0 of 1"
    in the UI before the real bar appears."""
    qm = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        job = await _submit(qm, steps=25)
        marker = {"prompt_id": job.id, "nodes": {"3": {"value": 0, "max": 1, "state": "running"}}}
        assert qm._apply_progress(job, "progress_state", marker) is False
        assert job.progress_max == 0

        real = {"prompt_id": job.id, "nodes": {"3": {"value": 1, "max": 25, "state": "running"}}}
        assert qm._apply_progress(job, "progress_state", real) is True
        assert (job.progress_value, job.progress_max) == (1, 25)

        # a genuine single-step job still reports, since nothing better exists
        one = await _submit(qm, steps=1)
        assert qm._apply_progress(
            one, "progress_state",
            {"prompt_id": one.id, "nodes": {"3": {"value": 1, "max": 1}}}) is True

        await asyncio.wait_for(job._done.wait(), 10)
        await asyncio.wait_for(one._done.wait(), 10)
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_position_counts_jobs_ahead_not_place_in_line(tmp_db_path, tmp_output_dir):
    """The UI says "N ahead of you" verbatim, so N has to be true. The first
    job into an empty queue has nobody in front of it."""
    comfy = FakeComfy()
    comfy.mode = "hang"        # keeps the GPU held so positions stay put
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        a = await _submit(qm)
        assert qm.position_of(a.id) == 0        # nothing ahead: it starts now
        await asyncio.sleep(0.05)
        assert qm._active is a
        assert qm.position_of(a.id) == 0        # running

        b = await _submit(qm)
        c = await _submit(qm)
        assert qm.position_of(b.id) == 1        # one ahead: the running job
        assert qm.position_of(c.id) == 2
        assert [j["position"] for j in qm.snapshot()["jobs"]] == [0, 1, 2]
        assert qm.position_of("nope") is None
    finally:
        await qm.stop()


# --- checkpoint-swap-aware ordering (Phase 5) -------------------------------
def _swap_qm_job(qm, checkpoint: str):
    """A job pinned to a given checkpoint, without needing that model on disk."""
    w = qm.reg.require("txt2img-sdxl")
    params = w.resolve({"prompt": "x", "steps": 4})
    params["checkpoint"] = checkpoint
    return Job(workflow_id=w.key, params=params)


@pytest.mark.asyncio
async def test_a_job_needing_no_swap_is_promoted(tmp_db_path, tmp_output_dir):
    """A swap costs 30s+, so within a bounded window a job whose model is
    already loaded goes first."""
    qm = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        qm._resident_checkpoint = "A"
        qm._pending = [_swap_qm_job(qm, "B"), _swap_qm_job(qm, "B"), _swap_qm_job(qm, "A")]
        wanted = qm._pending[2]
        assert qm._pick_next() is wanted
        assert [j.skips for j in qm._pending] == [1, 1]   # the two it passed
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_nothing_starves_however_many_swaps_it_costs(tmp_db_path, tmp_output_dir):
    """Once a job has been passed over enough times it runs next, whatever it
    costs -- otherwise a steady trickle of same-model work could hold it
    forever."""
    qm = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        qm._resident_checkpoint = "A"
        starved = _swap_qm_job(qm, "B")
        starved.skips = qm.cfg.queue_max_skips
        qm._pending = [starved, _swap_qm_job(qm, "A")]
        assert qm._pick_next() is starved
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_promotion_only_reaches_inside_the_window(tmp_db_path, tmp_output_dir):
    """A job can only ever be overtaken by jobs near the front of the queue."""
    qm = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        qm._resident_checkpoint = "A"
        far = _swap_qm_job(qm, "A")
        qm._pending = [_swap_qm_job(qm, "B")
                       for _ in range(qm.cfg.queue_reorder_window + 2)] + [far]
        head = qm._pending[0]
        assert qm._pick_next() is head      # `far` is out of reach; FIFO wins
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_one_checkpoint_means_plain_fifo(tmp_db_path, tmp_output_dir):
    """The Phase 1 promise: with a single model in play this does nothing."""
    qm = await _make_qm(tmp_db_path, tmp_output_dir, FakeComfy())
    try:
        qm._resident_checkpoint = "A"
        qm._pending = [_swap_qm_job(qm, "A") for _ in range(5)]
        order = [qm._pick_next() for _ in range(5)]
        assert [j.id for j in order] == [j.id for j in order]
        assert all(j.skips == 0 for j in order)
    finally:
        await qm.stop()
