"""GPU power and state via nvidia-smi, lifted from Llamacracy. The laptop's
mobile 1660 Ti does report power.draw (checked: ~2.4W idle), but there is no
cumulative energy counter, so draw is sampled while a job holds the GPU and
the mean is multiplied by the occupancy. CPU energy is estimated in config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

log = logging.getLogger("alchemyui.power")

_HAVE_NVIDIA_SMI = shutil.which("nvidia-smi") is not None


async def _query(fields: str) -> list[str] | None:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return [x.strip() for x in out.decode().strip().splitlines()[0].split(",")]
    except (TimeoutError, OSError, ValueError, IndexError):
        return None
    finally:
        # A wedged driver makes nvidia-smi hang; on a timeout (or when the
        # sampler is cancelled mid-call) kill and reap it, or one zombie piles
        # up every few seconds for as long as the driver stays stuck.
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())


class GpuPowerSampler:
    """Background poller. Start it when a job takes the GPU, stop it at the
    end, then read `.mean` (None if nvidia-smi was unavailable)."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._samples: list[float] = []

    async def _loop(self) -> None:
        while True:
            row = await _query("power.draw")
            try:
                if row:
                    self._samples.append(float(row[0]))
            except ValueError:
                pass            # "[N/A]" on a GPU that can't report it
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if _HAVE_NVIDIA_SMI and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="gpu-power-sampler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def mean(self) -> float | None:
        if not self._samples:
            return None
        return round(sum(self._samples) / len(self._samples), 1)


_STATS = ("memory.used,memory.total,temperature.gpu,power.draw,utilization.gpu,"
          "enforced.power.limit,power.default_limit")


def _num(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None             # "[N/A]" on a GPU that cannot report it


def parse_stats(row: list[str] | None) -> dict:
    """One nvidia-smi row -> the admin page's live tiles.

    `capped` is the reading that matters most on a laptop. Unplugged, the
    driver clamps this 1660 Ti to 10 of its 80 watts (P8, 300 MHz, "SW Power
    Cap: Active") and every generation runs ~10x slower. From the outside
    that looks exactly like a broken app.
    """
    if not row or len(row) < 5:
        return {"available": False}
    used, total, temp, power, util = (_num(v) for v in row[:5])
    if None in (used, total, power):
        return {"available": False}
    limit = _num(row[5]) if len(row) > 5 else None
    default = _num(row[6]) if len(row) > 6 else None
    return {
        "available": True,
        "vram_used_mib": int(used), "vram_total_mib": int(total),
        "temp_c": temp, "power_w": power, "util_pct": util,
        "power_limit_w": limit, "default_limit_w": default,
        "capped": bool(limit and default and limit < 0.5 * default),
    }


def on_mains() -> bool | None:
    """True on AC, False on battery, None where there is no such supply."""
    for supply in Path("/sys/class/power_supply").glob("*"):
        try:
            if (supply / "type").read_text().strip() == "Mains":
                return (supply / "online").read_text().strip() == "1"
        except OSError:
            continue
    return None


async def gpu_stats() -> dict:
    """One snapshot for the admin page's live tiles."""
    stats = parse_stats(await _query(_STATS))
    stats["on_mains"] = on_mains()
    return stats
