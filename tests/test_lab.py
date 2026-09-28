"""The Photoreal Lab: a grid of settings rendered at low priority, compared on
shared seeds, starred, tallied, and adopted as a model's profile."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from alchemyui.lab import MAX_CELLS, LabInvalid, expand, load_presets
from alchemyui.registry import load_registry
from tests.conftest import REPO, A, B, wait_for

RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
EPIC = "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"
PRESETS = load_presets(REPO / "workflows")


@pytest.fixture
def reg():
    return load_registry(REPO / "workflows")


def spec(**kw):
    base = {"checkpoints": [RV6, EPIC], "prompts": ["portrait", "food"], "seeds": 2,
            "axes": [{"param": "cfg", "values": [3.5, 5, 7]}]}
    base.update(kw)
    return base


# --- expansion ----------------------------------------------------------------
def test_expansion_is_every_combination_grouped_by_model(reg):
    cells, norm = expand(spec(), reg, PRESETS)
    assert len(cells) == 2 * 2 * 2 * 3
    # grouped by model, so the GPU swaps once per model rather than per cell
    assert [c.checkpoint for c in cells] == [RV6] * 12 + [EPIC] * 12
    # the seeds are drawn once and shared, so only the settings differ
    assert len(norm["seed_values"]) == 2
    assert {c.seed for c in cells} == set(norm["seed_values"])
    assert sorted({c.combo["cfg"] for c in cells}) == [3.5, 5.0, 7.0]
    # a cell is the model's own profile with the combination laid over it
    c = next(c for c in cells if c.checkpoint == RV6 and c.combo["cfg"] == 7.0)
    assert c.settings["cfg"] == 7.0 and c.settings["steps"] == 25
    assert c.settings["sampler"] == "dpmpp_2m" and c.settings["scheduler"] == "karras"
    assert c.settings["negative"] == PRESETS["negatives"]["house"]["text"]


def test_sampler_look_and_negative_axes_become_real_settings(reg):
    cells, _ = expand(spec(checkpoints=[RV6], prompts=["food"], seeds=1, axes=[
        {"param": "sampler", "values": ["euler_ancestral normal"]},
        {"param": "look", "values": ["raw"]}]), reg, PRESETS)
    (c,) = cells
    assert (c.settings["sampler"], c.settings["scheduler"]) == ("euler_ancestral", "normal")
    assert c.settings["look"] == PRESETS["looks"]["raw"]["text"]
    assert c.combo == {"sampler": "euler_ancestral normal", "look": "raw"}
    (n,) = expand(spec(checkpoints=[RV6], prompts=["food"], seeds=1,
                       axes=[{"param": "negative", "values": ["strict"]}]), reg, PRESETS)[0]
    assert n.settings["negative"] == PRESETS["negatives"]["strict"]["text"]


def test_a_custom_prompt_joins_the_presets(reg):
    cells, norm = expand(spec(checkpoints=[RV6], prompts=[], custom="a red bicycle", seeds=1,
                              axes=[]), reg, PRESETS)
    assert [c.prompt_key for c in cells] == ["custom"]
    assert norm["custom"] == "a red bicycle"


def test_a_run_is_capped(reg):
    everything = [c.name for c in reg.switchable()]
    all_prompts = [p["key"] for p in PRESETS["prompts"]]
    cells, _ = expand(spec(checkpoints=everything, prompts=all_prompts, seeds=3, axes=[]),
                      reg, PRESETS)
    assert len(cells) == MAX_CELLS
    with pytest.raises(LabInvalid, match="more than"):
        expand(spec(checkpoints=everything, prompts=all_prompts, seeds=3,
                    axes=[{"param": "cfg", "values": [4, 6]}]), reg, PRESETS)


@pytest.mark.parametrize("bad", [
    {"checkpoints": []},
    {"checkpoints": ["SD1.5/v1-5-pruned-emaonly.safetensors"]},
    {"prompts": ["nope"]},
    {"prompts": [], "custom": ""},
    {"seeds": 0},
    {"seeds": 4},
    {"axes": [{"param": "denoise", "values": [0.5]}]},
    {"axes": [{"param": "cfg", "values": [5]}, {"param": "cfg", "values": [6]}]},
    {"axes": [{"param": "cfg", "values": []}]},
    {"axes": [{"param": "sampler", "values": ["warp_drive karras"]}]},
    {"axes": [{"param": "look", "values": ["sepia"]}]},
    {"axes": [{"param": "steps", "values": ["lots"]}]},
    {"axes": [{"param": "cfg", "values": [1]}, {"param": "steps", "values": [2]},
              {"param": "look", "values": ["raw"]}]},
])
def test_what_cannot_run_is_refused(reg, bad):
    with pytest.raises(LabInvalid):
        expand(spec(**bad), reg, PRESETS)


# --- over HTTP, as the admin -----------------------------------------------------
pytestmark_http = pytest.mark.env(DEV_MODE="", ADMIN_EMAILS="alice@example.com")


def _run_done(client, run_id):
    def check():
        r = client.get(f"/api/lab/runs/{run_id}", headers=A).json()
        return r if r["state"] == "done" else None
    return wait_for(client, check, timeout=20)


@pytestmark_http
def test_a_run_renders_every_cell_and_its_images_stay_out_of_the_library(client):
    r = client.post("/api/lab/runs", headers=A, json=spec(
        checkpoints=[RV6], prompts=["food"], seeds=1,
        axes=[{"param": "cfg", "values": [4, 6]}]))
    assert r.status_code == 200, r.text
    run = _run_done(client, r.json()["id"])
    assert run["progress"] == {"done": 2, "failed": 0, "total": 2}
    assert all(c["image"]["url"] for c in run["cells"])
    cfgs = sorted(c["settings"]["cfg"] for c in run["cells"])
    assert cfgs == [4.0, 6.0]
    # the jobs really ran with the cell's settings
    params = [json.loads(p[0]) for p in client.db._conn.execute(
        "SELECT params_json FROM jobs WHERE lane = 'lab'").fetchall()]
    assert sorted(p["cfg"] for p in params) == [4.0, 6.0]
    assert {p["checkpoint"] for p in params} == {RV6}
    assert client.get("/api/images", headers=A).json()["images"] == []
    assert client.get("/api/lab/runs", headers=A).json()["runs"][0]["id"] == run["id"]


@pytestmark_http
def test_the_estimate_counts_images_and_time_before_starting(client):
    r = client.post("/api/lab/estimate", headers=A, json=spec())
    assert r.status_code == 200, r.text
    assert r.json()["cells"] == 24 and r.json()["seconds"] > 0
    assert client.post("/api/lab/estimate", headers=A,
                       json=spec(seeds=9)).status_code == 400


@pytestmark_http
def test_stars_tally_and_the_winner_becomes_the_default(client):
    r = client.post("/api/lab/runs", headers=A, json=spec(
        checkpoints=[RV6], prompts=["food", "portrait"], seeds=1,
        axes=[{"param": "cfg", "values": [4, 6]}]))
    run = _run_done(client, r.json()["id"])
    six = [c for c in run["cells"] if c["settings"]["cfg"] == 6.0]
    for c in six:
        assert client.post(f"/api/lab/cells/{c['id']}/star", headers=A,
                           json={"stars": 1}).status_code == 200
    run = client.get(f"/api/lab/runs/{run['id']}", headers=A).json()
    (tally,) = run["tallies"]
    assert tally["checkpoint"] == RV6
    assert tally["rows"][0]["combo"] == {"cfg": 6.0} and tally["rows"][0]["stars"] == 2

    cell = tally["rows"][0]["cell_id"]
    r = client.post(f"/api/lab/cells/{cell}/adopt", headers=A)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["cfg"] == 6.0
    # everyone's next job on that model runs with it...
    from alchemyui.threads import get_engine
    assert get_engine().reg.checkpoint(RV6).defaults["cfg"] == 6.0
    # ...and the model menu shows the adopted image, to anyone
    by = {m["name"]: m for m in client.get("/api/models", headers=B).json()["models"]}
    assert by[RV6]["cover_url"]
    assert client.get(by[RV6]["cover_url"], headers=B).status_code == 200

    profiles = {p["name"]: p for p in client.get("/api/lab/profiles", headers=A).json()["profiles"]}
    assert profiles[RV6]["settings"]["cfg"] == 6.0
    assert profiles[RV6]["base"]["cfg"] == 5.0
    assert client.delete(f"/api/lab/profiles?name={RV6}", headers=A).status_code == 200
    assert get_engine().reg.checkpoint(RV6).defaults["cfg"] == 5.0


@pytestmark_http
def test_the_lab_is_for_admins_only(client):
    for method, path in (("get", "/api/lab/runs"), ("get", "/api/lab/presets"),
                         ("post", "/api/lab/runs"), ("get", "/api/lab/profiles"),
                         ("get", "/lab")):
        r = getattr(client, method)(path, headers=B, **({"json": spec()} if method == "post" else {}))
        assert r.status_code == 403, (method, path, r.status_code)
    assert client.get("/api/lab/presets", headers=A).json()["prompts"][0]["key"] == "portrait"


@pytestmark_http
def test_a_cancelled_run_stops_its_cells(client):
    client.comfy.mode = "hang"
    r = client.post("/api/lab/runs", headers=A, json=spec(checkpoints=[RV6], prompts=["food"],
                                                        seeds=1, axes=[]))
    rid = r.json()["id"]
    assert client.post(f"/api/lab/runs/{rid}/cancel", headers=A).status_code == 200
    run = wait_for(client, lambda: (lambda x: x if x["cells"][0]["state"] == "cancelled" else None)(
        client.get(f"/api/lab/runs/{rid}", headers=A).json()))
    assert run["state"] == "cancelled"


# --- the library's model filter ------------------------------------------------------
@pytestmark_http
def test_the_library_filters_by_model(client):
    for ck in (RV6, EPIC):
        client.post("/api/generate", headers=A, json={"workflow_id": "txt2img-sdxl",
                                                      "prompt": "x", "checkpoint": ck})
    wait_for(client, lambda: len(client.get("/api/images", headers=A).json()["images"]) == 2)
    got = client.get(f"/api/images?checkpoint={EPIC}", headers=A).json()["images"]
    assert [i["params"]["checkpoint"] for i in got] == [EPIC]


# --- preemption, below HTTP ------------------------------------------------------------
async def test_a_preempted_cell_is_requeued_and_still_finishes(engine_harness):
    from alchemyui.lab import LabEngine

    h = engine_harness
    h.comfy.delay = 0.03
    h.comfy.steps = 20
    lab = LabEngine(h.db, h.qm, h.qm.reg, h.settings)
    await lab.start()
    run = await lab.create_run(h.alice, spec(checkpoints=[RV6], prompts=["food"], seeds=1,
                                             axes=[]))
    first_job = (await h.db.fetch_one("SELECT job_id FROM lab_cells WHERE run_id = ?",
                                      (run["id"],)))["job_id"]
    for _ in range(100):
        if h.qm._active is not None and h.qm._active.id == first_job:
            break
        await asyncio.sleep(0.01)
    w = h.qm.reg.require("txt2img-sdxl")
    person = await h.qm.submit(w, w.resolve({"prompt": "mine"}), h.bob)
    await asyncio.wait_for(person._done.wait(), 10)
    for _ in range(500):
        if (await lab.run_public(run["id"]))["state"] == "done":
            break
        await asyncio.sleep(0.02)
    done = await lab.run_public(run["id"])
    assert done["state"] == "done"
    (cell,) = done["cells"]
    assert cell["state"] == "done" and cell["job_id"] != first_job
    # the person's job ran before the cell's second attempt
    rows = {r["id"]: r for r in await h.db.fetch_all("SELECT * FROM jobs")}
    assert rows[first_job]["status"] == "cancelled"
    assert rows[person.id]["finished_at"] <= rows[cell["job_id"]]["started_at"]


def test_presets_parse_and_every_prompt_aspect_is_offered_by_every_model(reg):
    for p in PRESETS["prompts"]:
        for c in reg.switchable():
            assert p["aspect"] in c.aspects, (p["key"], c.name)
    choices = reg.require("txt2img-sdxl").choices
    for pair in PRESETS["samplers"]:
        s, sched = pair.split()
        assert s in choices["sampler"] and sched in choices["scheduler"]
    assert Path(REPO / "workflows" / "lab.json").exists()
