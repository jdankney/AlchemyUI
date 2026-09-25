"""GPU power and state via nvidia-smi, lifted from Llamacracy. The laptop's
mobile 1660 Ti does report power.draw (checked: ~2.4W idle), but there is no
cumulative energy counter, so draw is sampled while a job holds the GPU and
the mean is multiplied by the occupancy. CPU energy is estimated in config.
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("alchemyui.power")

_HAVE_NVIDIA_SMI = shutil.which("nvidia-smi") is not None


async def _query(fields: str) -> list[str] | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return [x.strip() for x in out.decode().strip().splitlines()[0].split(",")]
    except (TimeoutError, OSError, ValueError, IndexError):
        return None


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


async def gpu_stats() -> dict:
    """One snapshot for the admin page's live tiles."""
    row = await _query("memory.used,memory.total,temperature.gpu,power.draw,utilization.gpu")
    try:
        used, total, temp, power, util = row  # type: ignore[misc]
        return {"vram_used_mib": int(float(used)), "vram_total_mib": int(float(total)),
                "temp_c": float(temp), "power_w": float(power), "util_pct": float(util),
                "available": True}
    except (TypeError, ValueError):
        return {"available": False}
