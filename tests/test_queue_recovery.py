"""The queue against a ComfyUI that behaves like the real one where it matters:

- POST /prompt takes a moment, so a cancel can land while it is in flight;
- a targeted /interrupt only stops the prompt that is *running*;
- /queue delete only removes a prompt that is still *pending*;
- the WebSocket can drop, and ComfyUI can finish (or forget) work meanwhile.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from alchemyui.queue import JobState
from tests.conftest import FakeComfy
from tests.test_queue import _make_qm, _submit


class RealisticComfy(FakeComfy):
    def __init__(self):
        super().__init__()
        self.submit_latency = 0.05
        self.pending: set[str] = set()
        self.running: set[str] = set()
        self.completed: list[str] = []
        self.forgotten: set[str] = set()

    async def submit(self, graph, prompt_id):
        await asyncio.sleep(self.submit_latency)
        self.pending.add(prompt_id)
        return await super().submit(graph, prompt_id)

    async def interrupt(self, prompt_id):
        self.interrupts.append(prompt_id)
        if prompt_id in self.running:
            self._stopped.add(prompt_id)
        return True

    async def drop_pending(self, prompt_id):
        self.drops.append(prompt_id)
        self.pending.discard(prompt_id)
        return True

    async def knows(self, prompt_id):
        return prompt_id in self.pending or prompt_id in self.running

    async def history(self, prompt_id):
        return {"outputs": {"9": {"images": list(self.images)}}} \
            if prompt_id in self.completed else {}

    async def _drive(self, pid):
        await asyncio.sleep(0.02)          # sits in ComfyUI's queue briefly
        if pid not in self.pending:
            return                          # dropped before it started
        self.pending.discard(pid)
        self.running.add(pid)
        try:
            await self._emit({"type": "execution_start", "data": {"prompt_id": pid}})
            for i in range(1, self.steps + 1):
                await asyncio.sleep(self.delay)
                if pid in self.forgotten:
                    return                  # ComfyUI restarted: no events, no history
                if pid in self._stopped:
                    await self._emit({"type": "execution_interrupted",
                                      "data": {"prompt_id": pid}})
                    return
                await self._emit({"type": "progress_state", "data": {
                    "prompt_id": pid, "nodes": {"3": {"value": i, "max": self.steps}}}})
            self.completed.append(pid)
            await self._emit({"type": "executing", "data": {"prompt_id": pid, "node": None}})
        finally:
            self.running.discard(pid)


async def _submit_lab(qm, steps=20):
    w = qm.reg.require("txt2img-sdxl")
    return await qm.submit(w, w.resolve({"prompt": "a cat", "steps": steps}),
                           lane="lab", lab_run_id="r")


@pytest.mark.asyncio
async def test_a_cancel_during_the_submit_post_never_runs_to_completion(
        tmp_db_path, tmp_output_dir):
    comfy = RealisticComfy()
    comfy.steps, comfy.delay = 40, 0.02
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm, steps=40)
        await asyncio.sleep(0.01)                   # picked; POST /prompt in flight
        assert qm._active is job
        assert await qm.cancel(job.id) is True
        await asyncio.wait_for(job._done.wait(), 5)
        assert job.state is JobState.CANCELLED
        assert job.id not in comfy.completed        # ComfyUI did not render it anyway
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_preempting_a_just_picked_lab_job_frees_the_gpu(tmp_db_path, tmp_output_dir):
    comfy = RealisticComfy()
    comfy.steps, comfy.delay = 40, 0.02
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        lab = await _submit_lab(qm, steps=40)
        await asyncio.sleep(0.01)
        assert qm._active is lab
        mine = await _submit(qm)                    # a person arrives
        await asyncio.wait_for(mine._done.wait(), 5)
        assert lab.preempted and lab.state is JobState.CANCELLED
        assert lab.id not in comfy.completed
        assert mine.state is JobState.COMPLETED
    finally:
        await qm.stop()


class DroppingComfy(RealisticComfy):
    """The socket drops mid-job and comes back; what happened in the gap is lost."""

    def __init__(self, forget=False):
        super().__init__()
        self.blackout = False
        self.forget = forget

    async def _emit(self, event):
        if self.blackout:
            return
        await self._events.put(event)

    async def _drive(self, pid):
        self.pending.discard(pid)
        self.running.add(pid)
        await self._emit({"type": "execution_start", "data": {"prompt_id": pid}})
        await asyncio.sleep(0.02)
        await self._events.put({"type": "_ws_disconnected", "data": {}})
        self.blackout = True
        self.running.discard(pid)
        if not self.forget:
            self.completed.append(pid)              # finished while nobody listened
        await asyncio.sleep(0.05)
        self.blackout = False
        await self._events.put({"type": "_ws_connected", "data": {}})


@pytest.mark.asyncio
async def test_a_job_that_finished_during_a_socket_gap_is_collected(
        tmp_db_path, tmp_output_dir):
    comfy = DroppingComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 3)  # not the 900s timeout budget
        assert job.state is JobState.COMPLETED
        assert len(job.images) == 1
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_job_comfyui_forgot_fails_promptly(tmp_db_path, tmp_output_dir):
    comfy = DroppingComfy(forget=True)
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        job = await _submit(qm)
        await asyncio.wait_for(job._done.wait(), 3)
        assert job.state is JobState.FAILED
        assert "lost" in job.error_text
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_database_error_outside_the_run_does_not_kill_the_worker(
        tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    real = qm._persist_job
    calls = {"n": 0}

    async def flaky(job):
        calls["n"] += 1
        if calls["n"] == 2:                         # the worker's "running" write
            raise sqlite3.OperationalError("database is locked")
        await real(job)

    qm._persist_job = flaky
    try:
        a = await _submit(qm)
        await asyncio.wait_for(a._done.wait(), 3)
        assert a.state is JobState.FAILED
        assert qm._active is None
        b = await _submit(qm)
        await asyncio.wait_for(b._done.wait(), 3)
        assert b.state is JobState.COMPLETED
    finally:
        await qm.stop()
