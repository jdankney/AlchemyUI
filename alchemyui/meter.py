"""Metering arithmetic, kept pure so it can be tested to the cent.

    energy_wh = (gpu_watts_mean + NON_GPU_LOAD_WATTS) x occupancy_seconds / 3600
    cost_usd  = energy_wh / 1000 x rate          (rate: TOU hour, or flat)

Occupancy runs from the worker taking the job to its end, model load
included; waiting in the queue is never counted. Everything that actually
held the GPU is attributed to its owner, failures and cancellations too --
Llamacracy zeroes those because it bills, and AlchemyUI only tracks. The
result is frozen on the job row and never recomputed, so a rate change
cannot rewrite history.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class Reading:
    occupancy_seconds: float
    gpu_watts_mean: float | None
    energy_wh: float
    rate_used: float
    cost_usd: float


def finalise(picked_at: float, finished_at: float, gpu_watts_mean: float | None,
             settings: Settings) -> Reading:
    # Rounded before use, not after: a frozen row must agree with itself, so
    # energy is derived from exactly the occupancy that gets stored.
    occupancy = round(max(0.0, finished_at - picked_at), 3)
    gpu = gpu_watts_mean if gpu_watts_mean is not None else settings.gpu_fallback_watts
    energy = (gpu + settings.non_gpu_load_watts) * occupancy / 3600
    rate = settings.rate_for_hour(time.localtime(picked_at).tm_hour)
    return Reading(
        occupancy_seconds=occupancy,
        gpu_watts_mean=gpu_watts_mean,
        # Stored exactly (REAL is a double); rounding is for display, and a
        # rounded-then-stored pair would stop agreeing with each other.
        energy_wh=energy,
        rate_used=rate,
        cost_usd=energy / 1000 * rate,
    )
