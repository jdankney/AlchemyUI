"""Metering: visibility, not billing. Every job that held the GPU records its
occupancy, measured watts, energy and cost -- frozen on the row."""

from __future__ import annotations

import asyncio
import time

import pytest

from alchemyui.config import Settings
from alchemyui.meter import finalise
from tests.conftest import FakeComfy, add_user
from tests.test_queue import _make_qm


def test_energy_is_gpu_plus_the_rest_of_the_machine_over_occupancy():
    s = Settings()
    object.__setattr__(s, "non_gpu_load_watts", 20.0)
    object.__setattr__(s, "electricity_rate", 0.40)
    r = finalise(1000.0, 1360.0, 70.0, s)           # 6 minutes at 70 + 20 W
    assert r.occupancy_seconds == 360.0
    assert r.energy_wh == pytest.approx(9.0)        # 90 W x 0.1 h
    assert r.rate_used == 0.40
    assert r.cost_usd == pytest.approx(0.0036)      # 0.009 kWh x $0.40


def test_missing_power_reading_falls_back_rather_than_zero():
    s = Settings()
    object.__setattr__(s, "gpu_fallback_watts", 60.0)
    object.__setattr__(s, "non_gpu_load_watts", 0.0)
    r = finalise(0.0, 3600.0, None, s)
    assert r.gpu_watts_mean is None and r.energy_wh == pytest.approx(60.0)


def test_time_of_use_rate_is_taken_from_the_local_hour_it_ran():
    s = Settings()
    table = {str(h): (0.10 if h < 12 else 0.50) for h in range(24)}
    object.__setattr__(s, "tou_schedule", __import__("json").dumps(table))
    morning = time.mktime((2026, 9, 25, 9, 0, 0, 0, 0, -1))
    evening = time.mktime((2026, 9, 25, 19, 0, 0, 0, 0, -1))
    assert finalise(morning, morning + 60, 50, s).rate_used == 0.10
    assert finalise(evening, evening + 60, 50, s).rate_used == 0.50


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "error"])
async def test_every_job_that_ran_is_metered_and_frozen(tmp_db_path, tmp_output_dir, mode):
    comfy = FakeComfy()
    comfy.mode = mode
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        uid = await add_user(qm.db, "alice")
        w = qm.reg.require("txt2img-sdxl")
        job = await qm.submit(w, w.resolve({"prompt": "x", "steps": 4}), user_id=uid)
        await asyncio.wait_for(job._done.wait(), 10)
        row = await qm.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job.id,))
        s = qm.cfg
        assert row["gpu_watts_mean"] == 40.0                      # FixedSampler
        assert row["occupancy_seconds"] > 0
        assert row["energy_wh"] == pytest.approx(
            (40.0 + s.non_gpu_load_watts) * row["occupancy_seconds"] / 3600, rel=1e-3)
        assert row["cost_usd"] == pytest.approx(row["energy_wh"] / 1000 * row["rate_used"],
                                                rel=1e-3)
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_job_cancelled_while_queued_used_nothing(tmp_db_path, tmp_output_dir):
    comfy = FakeComfy()
    comfy.mode = "hang"
    qm = await _make_qm(tmp_db_path, tmp_output_dir, comfy)
    try:
        w = qm.reg.require("txt2img-sdxl")
        await qm.submit(w, w.resolve({"prompt": "a", "steps": 4}))
        waiting = await qm.submit(w, w.resolve({"prompt": "b", "steps": 4}))
        await qm.cancel(waiting.id)
        row = await qm.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (waiting.id,))
        assert row["energy_wh"] is None and row["picked_at"] is None
    finally:
        await qm.stop()
