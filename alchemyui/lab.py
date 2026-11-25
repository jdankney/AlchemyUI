"""The Photoreal Lab (Phase 7.5).

A run is a grid: models x prompts x seeds x up to two axes of settings. Every
cell is one image, rendered in the queue's low-priority lab lane -- it runs
only when no person is waiting and gives the GPU up the moment one arrives.
Seeds are drawn once per run and shared by every cell, so two cells differ
only in the settings being compared.

An admin stars the keepers; the tally sums stars per model and combination
across every prompt and seed; the winner is adopted as that model's profile
(profiles.py), for everyone, until it is reset.
"""

from __future__ import annotations

import itertools
import json
import logging
import random
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from .config import Settings
from .db import Database, now
from .planner.schema import Vocabulary
from .profiles import PROFILE_KEYS, save_profile
from .queue import TERMINAL, JobState, QueueManager
from .registry import MAX_SEED, Registry, WorkflowInfo

log = logging.getLogger("alchemyui.lab")

MAX_CELLS = 96
MAX_MODELS = 4
MAX_SEEDS = 3
MAX_AXES = 2
MAX_VALUES = 6
AXES = ("cfg", "steps", "sampler", "look", "negative")


class LabInvalid(ValueError):
    """The run cannot be made. The message is shown to the admin."""


@dataclass
class Cell:
    seq: int
    checkpoint: str
    prompt_key: str
    seed: int
    combo: dict                     # the axis values of this cell, as picked
    settings: dict = field(default_factory=dict)   # the full profile it runs with


@lru_cache
def load_presets(workflows_dir: str | Path) -> dict:
    return json.loads((Path(workflows_dir) / "lab.json").read_text())


def _txt2img(reg: Registry) -> WorkflowInfo:
    """The workflow a chat "generate" runs, so the Lab tunes exactly that."""
    return reg.require(Vocabulary.from_registry(reg, 1).workflows["generate"])


def _axis_value(param: str, value, presets: dict, choices: dict):
    """One axis value, checked and normalised; LabInvalid if it cannot run."""
    try:
        if param == "cfg":
            v = round(float(value), 2)
            if not 1 <= v <= 15:
                raise ValueError
            return v
        if param == "steps":
            v = int(value)
            if not 1 <= v <= 80:
                raise ValueError
            return v
    except (TypeError, ValueError):
        raise LabInvalid(f"{value!r} is not a usable {param}") from None
    if param == "sampler":
        parts = str(value).split()
        if (str(value) not in presets["samplers"] or len(parts) != 2
                or parts[0] not in choices.get("sampler", ())
                or parts[1] not in choices.get("scheduler", ())):
            raise LabInvalid(f"{value!r} is not a sampler the Lab offers")
        return str(value)
    table = presets["looks" if param == "look" else "negatives"]
    if value not in table:
        raise LabInvalid(f"{value!r} is not a {param} the Lab offers")
    return value


def base_settings(reg: Registry, name: str, presets: dict, baseline: bool = False) -> dict:
    """What a model runs with today: its registry defaults with any adopted
    profile laid over them, filled out to every profile key. With
    `baseline`, what the registry file alone says."""
    ck = reg.base(name) if baseline else reg.checkpoint(name)
    wf = _txt2img(reg)
    d = {**wf.defaults, **(ck.defaults if ck else {})}
    return {
        "steps": int(d.get("steps") or 25),
        "cfg": float(d.get("cfg") or 6.5),
        "sampler": d.get("sampler") or "dpmpp_2m",
        "scheduler": d.get("scheduler") or "karras",
        "look": str(d.get("look") or ""),
        # the profile's own negative, else the one every job gets by default
        "negative": str(ck.defaults["negative"]) if ck and "negative" in ck.defaults
        else str(wf.defaults.get("negative") or presets["negatives"]["house"]["text"]),
    }


def _apply(settings: dict, param: str, value, presets: dict) -> dict:
    s = dict(settings)
    if param in ("cfg", "steps"):
        s[param] = value
    elif param == "sampler":
        s["sampler"], s["scheduler"] = value.split()
    elif param == "look":
        s["look"] = presets["looks"][value]["text"]
    elif param == "negative":
        s["negative"] = presets["negatives"][value]["text"]
    return s


def expand(spec: dict, reg: Registry, presets: dict) -> tuple[list[Cell], dict]:
    """A run's spec, checked and multiplied out into cells. Returns the cells
    and the normalised spec (with the drawn seeds) that is stored with it."""
    switchable = [c.name for c in reg.switchable()]
    models = list(dict.fromkeys(spec.get("checkpoints") or []))
    if not 1 <= len(models) <= MAX_MODELS:
        raise LabInvalid(f"pick one to {MAX_MODELS} models")
    for m in models:
        if m not in switchable:
            raise LabInvalid(f"{m} is not a model the Lab can tune")
    models.sort(key=switchable.index)

    known = {p["key"]: p for p in presets["prompts"]}
    prompts = list(dict.fromkeys(spec.get("prompts") or []))
    for k in prompts:
        if k not in known:
            raise LabInvalid(f"{k!r} is not one of the Lab's prompts")
    custom = " ".join(str(spec.get("custom") or "").split())[:400]
    if custom:
        prompts.append("custom")
    if not prompts:
        raise LabInvalid("pick at least one prompt, or write your own")

    try:
        n_seeds = int(spec.get("seeds") or 0)
    except (TypeError, ValueError):
        n_seeds = 0
    if not 1 <= n_seeds <= MAX_SEEDS:
        raise LabInvalid(f"use one to {MAX_SEEDS} seeds")
    seeds = [int(s) for s in (spec.get("seed_values") or [])][:n_seeds]
    while len(seeds) < n_seeds:
        seeds.append(random.randint(0, MAX_SEED))

    choices = _txt2img(reg).choices
    axes = []
    for a in spec.get("axes") or []:
        param = (a or {}).get("param")
        if param not in AXES:
            raise LabInvalid(f"{param!r} is not something the Lab can vary")
        if any(param == x["param"] for x in axes):
            raise LabInvalid(f"{param} is already an axis")
        values = list(dict.fromkeys(_axis_value(param, v, presets, choices)
                                    for v in (a.get("values") or [])))
        if not 1 <= len(values) <= MAX_VALUES:
            raise LabInvalid(f"give {param} one to {MAX_VALUES} values")
        axes.append({"param": param, "values": values})
    if len(axes) > MAX_AXES:
        raise LabInvalid(f"at most {MAX_AXES} axes")

    combos = list(itertools.product(*[a["values"] for a in axes])) or [()]
    total = len(models) * len(prompts) * len(seeds) * len(combos)
    if total > MAX_CELLS:
        raise LabInvalid(f"that is {total} images, more than a run can hold ({MAX_CELLS})")

    cells: list[Cell] = []
    for m in models:
        base = base_settings(reg, m, presets)
        for pk in prompts:
            for seed in seeds:
                for values in combos:
                    combo = {a["param"]: v for a, v in zip(axes, values, strict=True)}
                    s = base
                    for param, v in combo.items():
                        s = _apply(s, param, v, presets)
                    cells.append(Cell(len(cells) + 1, m, pk, seed, combo, s))
    norm = {"checkpoints": models, "prompts": [p for p in prompts if p != "custom"],
            "custom": custom, "seeds": n_seeds, "seed_values": seeds, "axes": axes,
            "title": " ".join(str(spec.get("title") or "").split())[:80]}
    return cells, norm


def _label(combo: dict, presets: dict) -> str:
    parts = []
    for k, v in combo.items():
        if k in ("look", "negative"):
            parts.append(f"{k} {presets['looks' if k == 'look' else 'negatives'][v]['label']}")
        else:
            parts.append(f"{k} {v}")
    return " · ".join(parts) or "as it is"


class LabEngine:
    def __init__(self, db: Database, qm: QueueManager, reg: Registry, settings: Settings):
        self.db, self.qm, self.reg, self.cfg = db, qm, reg, settings
        self.presets = load_presets(settings.workflows_dir)

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self.qm.add_listener(self._on_job)
        # A restart restores queued lab jobs with the rest of the queue; a cell
        # whose job was running when we went down comes back failed, and is
        # simply rendered again.
        for run in await self.db.fetch_all("SELECT * FROM lab_runs WHERE state = 'running'"):
            for cell in await self.db.fetch_all(
                    "SELECT c.*, j.status AS job_status FROM lab_cells c "
                    "LEFT JOIN jobs j ON j.id = c.job_id "
                    "WHERE c.run_id = ? AND c.state = 'queued'", (run["id"],)):
                if cell["job_status"] not in (JobState.QUEUED.value, JobState.RUNNING.value):
                    try:
                        await self._submit(run, cell)
                    except Exception:  # noqa: BLE001 -- never let one cell stop the app booting
                        # Its checkpoint or prompt left the registry since the
                        # run began: fail the cell rather than the lifespan.
                        log.exception("lab run %s: cell %s cannot be resumed",
                                      run["id"], cell["id"])
                        await self.db.execute(
                            "UPDATE lab_cells SET state = 'failed' WHERE id = ?", (cell["id"],))
            await self._maybe_finish(run["id"])

    # -- making a run -------------------------------------------------------
    def estimate(self, spec: dict) -> dict:
        cells, _ = expand(spec, self.reg, self.presets)
        wf = _txt2img(self.reg)
        seconds, resident = 0.0, self.qm._resident_checkpoint
        for c in cells:
            seconds += self.qm.estimate(wf.key, c.settings["steps"], c.checkpoint != resident,
                                        c.checkpoint)
            resident = c.checkpoint
        return {"cells": len(cells), "seconds": round(seconds)}

    async def create_run(self, user_id: int, spec: dict) -> dict:
        cells, norm = expand(spec, self.reg, self.presets)
        run_id = uuid.uuid4().hex
        ts = now()
        title = norm["title"] or self._title(norm)
        await self.db.execute(
            "INSERT INTO lab_runs (id, created_by, title, spec_json, state, created_at) "
            "VALUES (?, ?, ?, ?, 'running', ?)", (run_id, user_id, title, json.dumps(norm), ts))
        for c in cells:
            await self.db.execute(
                "INSERT INTO lab_cells (id, run_id, seq, checkpoint, prompt_key, seed, combo_json, "
                "  settings_json, state, created_at) VALUES (?,?,?,?,?,?,?,?,'queued',?)",
                (uuid.uuid4().hex, run_id, c.seq, c.checkpoint, c.prompt_key, c.seed,
                 json.dumps(c.combo), json.dumps(c.settings), ts))
        run = await self.db.fetch_one("SELECT * FROM lab_runs WHERE id = ?", (run_id,))
        for cell in await self.db.fetch_all(
                "SELECT * FROM lab_cells WHERE run_id = ? ORDER BY seq", (run_id,)):
            await self._submit(run, cell)
        log.info("lab run %s: %d cells", run_id, len(cells))
        return await self.run_public(run_id)

    def _title(self, norm: dict) -> str:
        labels = [self.reg.checkpoint(m).label for m in norm["checkpoints"]]
        axes = " × ".join(a["param"] for a in norm["axes"]) or "defaults"
        return f"{', '.join(labels)} · {axes}"

    def _prompt_text(self, spec: dict, key: str) -> tuple[str, str]:
        if key == "custom":
            return spec["custom"], "1:1"
        p = next(p for p in self.presets["prompts"] if p["key"] == key)
        return p["prompt"], p["aspect"]

    async def _submit(self, run, cell) -> None:
        spec = json.loads(run["spec_json"])
        s = json.loads(cell["settings_json"])
        text, aspect = self._prompt_text(spec, cell["prompt_key"])
        wf = _txt2img(self.reg)
        base = self.reg.checkpoint(cell["checkpoint"])
        # The cell's settings become the checkpoint's for this one job, so the
        # workflow resolves them exactly as it would a real message's.
        ck = replace(base, defaults={**base.defaults, **{k: s[k] for k in
                                                         ("steps", "cfg", "sampler", "scheduler")}})
        prompt = f"{text}, {s['look']}" if s["look"] else text
        params = wf.resolve({"prompt": prompt, "negative": s["negative"], "aspect": aspect,
                             "seed": cell["seed"]}, ck)
        job = await self.qm.submit(wf, params, run["created_by"], lane="lab",
                                   lab_run_id=run["id"])
        await self.db.execute("UPDATE lab_cells SET job_id = ?, state = 'queued' WHERE id = ?",
                              (job.id, cell["id"]))

    # -- the queue hook -------------------------------------------------------
    async def _on_job(self, job) -> None:
        if job.lane != "lab":
            return
        cell = await self.db.fetch_one("SELECT * FROM lab_cells WHERE job_id = ?", (job.id,))
        if cell is None:
            return
        run = await self.db.fetch_one("SELECT * FROM lab_runs WHERE id = ?", (cell["run_id"],))
        if job.state == JobState.COMPLETED and job.images:
            await self.db.execute("UPDATE lab_cells SET state = 'done', image_id = ? WHERE id = ?",
                                  (job.images[0]["id"], cell["id"]))
        elif job.state == JobState.CANCELLED and job.preempted and run["state"] == "running":
            # A person needed the GPU. Go to the back of the lab lane.
            await self._submit(run, cell)
            return
        elif job.state == JobState.CANCELLED:
            await self.db.execute("UPDATE lab_cells SET state = 'cancelled' WHERE id = ?",
                                  (cell["id"],))
        else:
            await self.db.execute("UPDATE lab_cells SET state = 'failed' WHERE id = ?",
                                  (cell["id"],))
        await self._maybe_finish(run["id"])

    async def _maybe_finish(self, run_id: str) -> None:
        left = await self.db.fetch_one(
            "SELECT COUNT(*) AS n FROM lab_cells WHERE run_id = ? AND state = 'queued'", (run_id,))
        if left["n"] == 0:
            await self.db.execute(
                "UPDATE lab_runs SET state = CASE state WHEN 'running' THEN 'done' ELSE state END, "
                "  finished_at = COALESCE(finished_at, ?) WHERE id = ?", (now(), run_id))

    async def cancel_run(self, run_id: str) -> dict:
        run = await self._run(run_id)
        if run["state"] == "running":
            await self.db.execute(
                "UPDATE lab_runs SET state = 'cancelled', finished_at = ? WHERE id = ?",
                (now(), run_id))
            for cell in await self.db.fetch_all(
                    "SELECT * FROM lab_cells WHERE run_id = ? AND state = 'queued'", (run_id,)):
                live = self.qm._by_id.get(cell["job_id"])
                await self.db.execute("UPDATE lab_cells SET state = 'cancelled' WHERE id = ?",
                                      (cell["id"],))
                if live is not None and live.state not in TERMINAL:
                    await self.qm.cancel(live.id)
        return await self.run_public(run_id)

    # -- reading ----------------------------------------------------------------
    async def _run(self, run_id: str):
        run = await self.db.fetch_one("SELECT * FROM lab_runs WHERE id = ?", (run_id,))
        if run is None:
            raise KeyError(run_id)
        return run

    async def list_runs(self, limit: int = 50) -> list[dict]:
        rows = await self.db.fetch_all(
            "SELECT r.*, "
            "  (SELECT COUNT(*) FROM lab_cells c WHERE c.run_id = r.id) AS total, "
            "  (SELECT COUNT(*) FROM lab_cells c WHERE c.run_id = r.id AND c.state = 'done') AS done "
            "FROM lab_runs r ORDER BY r.created_at DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "title": r["title"], "state": r["state"],
                 "created_at": r["created_at"], "finished_at": r["finished_at"],
                 "done": r["done"], "total": r["total"]} for r in rows]

    async def run_public(self, run_id: str) -> dict:
        run = await self._run(run_id)
        spec = json.loads(run["spec_json"])
        rows = await self.db.fetch_all(
            "SELECT c.*, i.width, i.height, i.palette FROM lab_cells c "
            "LEFT JOIN images i ON i.id = c.image_id WHERE c.run_id = ? ORDER BY c.seq", (run_id,))
        cells = []
        for r in rows:
            live = self.qm._by_id.get(r["job_id"]) if r["job_id"] else None
            cells.append({
                "id": r["id"], "seq": r["seq"], "checkpoint": r["checkpoint"],
                "prompt_key": r["prompt_key"], "seed": r["seed"],
                "combo": json.loads(r["combo_json"]), "settings": json.loads(r["settings_json"]),
                "state": r["state"], "stars": r["stars"], "job_id": r["job_id"],
                "running": bool(live is not None and live.state == JobState.RUNNING),
                "image": {"id": r["image_id"], "url": f"/api/images/{r['image_id']}/file",
                          "width": r["width"], "height": r["height"],
                          "palette": json.loads(r["palette"]) if r["palette"] else []}
                if r["image_id"] else None,
            })
        progress = {"done": sum(c["state"] == "done" for c in cells),
                    "failed": sum(c["state"] == "failed" for c in cells),
                    "total": len(cells)}
        return {"id": run["id"], "title": run["title"], "state": run["state"], "spec": spec,
                "created_at": run["created_at"], "finished_at": run["finished_at"],
                "progress": progress, "cells": cells, "tallies": self._tallies(cells),
                "labels": {c["checkpoint"]: self.reg.checkpoint(c["checkpoint"]).label
                           for c in cells if self.reg.checkpoint(c["checkpoint"])}}

    def _tallies(self, cells: list[dict]) -> list[dict]:
        """Stars per model and combination, across every prompt and seed. The
        best-starred combination of each model comes first."""
        out = []
        for ck in dict.fromkeys(c["checkpoint"] for c in cells):
            groups: dict[str, dict] = {}
            for c in cells:
                if c["checkpoint"] != ck:
                    continue
                key = json.dumps(c["combo"], sort_keys=True)
                g = groups.setdefault(key, {"combo": c["combo"],
                                            "label": _label(c["combo"], self.presets),
                                            "stars": 0, "done": 0, "cell_id": None, "_best": -1})
                g["stars"] += c["stars"]
                if c["state"] == "done":
                    g["done"] += 1
                    # adopt from its best-starred image; its cover will be that
                    if c["stars"] > g["_best"]:
                        g["cell_id"], g["_best"] = c["id"], c["stars"]
            rows = sorted(groups.values(), key=lambda g: (-g["stars"], -g["done"]))
            for g in rows:
                del g["_best"]
            out.append({"checkpoint": ck, "rows": rows})
        return out

    # -- judging ----------------------------------------------------------------
    async def star(self, cell_id: str, stars: int) -> dict:
        stars = max(0, min(3, int(stars)))
        cell = await self.db.fetch_one("SELECT * FROM lab_cells WHERE id = ?", (cell_id,))
        if cell is None:
            raise KeyError(cell_id)
        await self.db.execute("UPDATE lab_cells SET stars = ? WHERE id = ?", (stars, cell_id))
        return {"id": cell_id, "stars": stars}

    async def adopt(self, cell_id: str, user_id: int) -> dict:
        cell = await self.db.fetch_one("SELECT * FROM lab_cells WHERE id = ?", (cell_id,))
        if cell is None:
            raise KeyError(cell_id)
        if cell["state"] != "done" or not cell["image_id"]:
            raise LabInvalid("only a finished image can be made the default")
        # Only what differs from the registry becomes the profile, so it reads
        # as what the Lab changed -- and a later registry edit to anything
        # else still takes effect.
        base = base_settings(self.reg, cell["checkpoint"], self.presets, baseline=True)
        settings = {k: v for k, v in json.loads(cell["settings_json"]).items()
                    if k in PROFILE_KEYS and base.get(k) != v}
        saved = await save_profile(self.db, self.reg, cell["checkpoint"], settings, user_id,
                                   cover_image_id=cell["image_id"])
        log.info("lab: %s adopted %s from cell %s", cell["checkpoint"], saved, cell_id)
        return {"checkpoint": cell["checkpoint"], "settings": saved}


_lab: LabEngine | None = None


def get_lab() -> LabEngine:
    if _lab is None:
        raise RuntimeError("lab not started")
    return _lab


async def start_lab(db, qm, reg, settings) -> LabEngine:
    global _lab
    _lab = LabEngine(db, qm, reg, settings)
    await _lab.start()
    return _lab


def stop_lab() -> None:
    global _lab
    _lab = None
