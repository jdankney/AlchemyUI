"""Phase 3: the quality chain.

Two ways in -- an Enhance mode that takes any image, and a high-quality toggle
that welds the chain onto a fresh generation. Both run the same nodes, so what
is checked here is the wiring, the VRAM ceiling that keeps a 6GB card alive,
and the multi-stage progress that stops the bar lying about a six-minute job.
"""

from __future__ import annotations

import io

from PIL import Image

from alchemyui.queue import QueueManager
from alchemyui.registry import Stage, load_registry
from tests.conftest import REPO
from tests.conftest import wait_for as _wait


def _png(size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (120, 90, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, data=None):
    r = client.post("/api/uploads", files={"file": ("src.png", data or _png(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()


# --- the chain's wiring ------------------------------------------------------
def test_the_chain_runs_the_nodes_the_spec_names():
    """4xNMKD upscale -> scale back -> tiled VAE round trip at low denoise ->
    FaceDetailer. Asserted against the shipped template, not a fixture."""
    w = load_registry(REPO / "workflows").require("enhance-sdxl")
    t = w.template
    kinds = {n["class_type"] for n in t.values()}
    assert {"UpscaleModelLoader", "ImageUpscaleWithModel", "ImageScaleBy",
            "VAEEncodeTiled", "KSampler", "VAEDecodeTiled",
            "UltralyticsDetectorProvider", "FaceDetailer"} <= kinds

    assert t["20"]["inputs"]["model_name"].startswith("4xNMKD")
    assert t["26"]["inputs"]["model_name"] == "bbox/face_yolov8m.pt"
    # tiled, with the spec's tile/overlap -- this is what survives 6GB
    for node in ("23", "25"):
        assert t[node]["inputs"]["tile_size"] == 512
        assert t[node]["inputs"]["overlap"] == 64
    # and the chain is what gets saved, not the pre-chain image
    assert t[w.output_node]["inputs"]["images"] == ["27", 0]


def test_the_chain_is_wired_end_to_end_in_order():
    t = load_registry(REPO / "workflows").require("enhance-sdxl").template
    assert t["21"]["inputs"]["image"] == ["10", 0]      # source -> upscaler
    assert t["22"]["inputs"]["image"] == ["21", 0]      # upscaler -> scale back
    assert t["23"]["inputs"]["pixels"] == ["22", 0]     # -> tiled encode
    assert t["24"]["inputs"]["latent_image"] == ["23", 0]   # -> hi-res sampler
    assert t["25"]["inputs"]["samples"] == ["24", 0]    # -> tiled decode
    assert t["27"]["inputs"]["image"] == ["25", 0]      # -> FaceDetailer


def test_hq_generation_chains_onto_the_base_image():
    t = load_registry(REPO / "workflows").require("txt2img-sdxl-hq").template
    assert t["8"]["class_type"] == "VAEDecode"          # the base decode
    assert t["21"]["inputs"]["image"] == ["8", 0]       # feeds the chain
    assert t["3"]["inputs"]["latent_image"] == ["5", 0]  # base still from an empty latent


def test_the_plain_generator_advertises_its_high_quality_twin(client):
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    assert ws["txt2img-sdxl"]["hq_variant"] == "txt2img-sdxl-hq"
    assert ws["txt2img-sdxl-hq"]["hq_variant"] is None       # no infinite ladder
    assert ws["img2img-sdxl"]["hq_variant"] is None


# --- the VRAM ceiling --------------------------------------------------------
def test_scale_by_is_reduced_so_a_large_source_cannot_oom_the_sampler(client):
    """The chain's output is 4 x source x scale_by. At the spec's 0.38 a
    1536px source would ask for 2334px, which a 6GB card will not do."""
    up = _upload(client, _png((1536, 1024)))
    r = client.post("/api/generate", json={
        "workflow_id": "enhance-sdxl", "prompt": "x", "image_id": up["id"]})
    p = r.json()["params"]
    assert p["scale_by"] < 0.38                    # reduced, not refused
    assert p["hq_output_edge"] <= 1600
    _wait(client, lambda: client.comfy.submitted)
    assert client.comfy.submitted[0][1]["22"]["inputs"]["scale_by"] == p["scale_by"]


def test_a_small_source_keeps_the_specs_scaling(client):
    up = _upload(client, _png((768, 768)))          # 4 x 768 x 0.38 = 1167, fits
    p = client.post("/api/generate", json={
        "workflow_id": "enhance-sdxl", "prompt": "x", "image_id": up["id"]}).json()["params"]
    assert p["scale_by"] == 0.38


def test_hq_generation_is_capped_from_its_own_canvas_size(client):
    """No upload to measure -- the ceiling comes from the chosen aspect."""
    p = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl-hq", "prompt": "x", "aspect": "16:9"}).json()["params"]
    assert p["hq_output_edge"] <= 1600              # 1344 wide would have been 2043


# --- one seed, whole job -----------------------------------------------------
def test_every_sampler_in_the_chain_takes_the_jobs_seed(client):
    p = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl-hq", "prompt": "x", "seed": 4242}).json()["params"]
    assert p["seed"] == p["hq_seed"] == p["face_seed"] == 4242
    _wait(client, lambda: client.comfy.submitted)
    _, g = client.comfy.submitted[0]
    assert g["3"]["inputs"]["seed"] == g["24"]["inputs"]["seed"] == g["27"]["inputs"]["seed"] == 4242


def test_chain_knobs_reach_their_nodes(client):
    client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl-hq", "prompt": "x",
        "refine": 0.42, "refine_steps": 18, "face_denoise": 0.33, "face_guide": 640})
    _wait(client, lambda: client.comfy.submitted)
    _, g = client.comfy.submitted[0]
    assert g["24"]["inputs"]["denoise"] == 0.42
    assert g["24"]["inputs"]["steps"] == 18
    assert g["27"]["inputs"]["denoise"] == 0.33
    assert g["27"]["inputs"]["guide_size"] == 640


# --- gallery image -> editable source ---------------------------------------
def test_a_generated_image_can_be_fed_back_in(client):
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "a tree"})
    img = _wait(client, lambda: client.get("/api/images").json()["images"])[0]

    up = client.post(f"/api/images/{img['id']}/reuse").json()
    assert up["width"] and up["height"]
    r = client.post("/api/generate", json={
        "workflow_id": "enhance-sdxl", "prompt": "a tree", "image_id": up["id"]})
    assert r.status_code == 200


def test_reusing_a_missing_image_says_so(client):
    assert client.post("/api/images/nope/reuse").status_code == 404


# --- multi-stage progress ----------------------------------------------------
def _stages():
    return (Stage("3", "Base image", 1.0), Stage("24", "Refining detail", 2.0),
            Stage("27", "Faces", 0.8))


def test_overall_progress_is_weighted_across_every_stage():
    st = _stages()
    total = 1.0 + 2.0 + 0.8
    # halfway through the base pass
    assert QueueManager._overall(st, 0, 5, 10) == (0.5 * 1.0) / total
    # base done, halfway through the hi-res pass
    assert QueueManager._overall(st, 1, 5, 10) == (1.0 + 1.0) / total
    # reaching the last stage means everything before it finished
    assert QueueManager._overall(st, 2, 0, 10) == 3.0 / total
    assert QueueManager._overall(st, 2, 10, 10) == 1.0


def test_a_single_stage_workflow_is_just_its_own_progress():
    st = (Stage("3", "", 1.0),)
    assert QueueManager._overall(st, 0, 3, 12) == 0.25


def test_progress_reports_the_stage_it_is_in(client):
    """The card says "Refining detail · 7/25", not "Step 7 of 25" -- three
    passes each counting to 25 would otherwise look like one stuck bar."""
    client.comfy.steps = 6
    client.comfy.delay = 0.02
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl-hq", "prompt": "x"})
    job = _wait(client, lambda: next(
        (j for j in client.get("/api/jobs").json()["jobs"]
         if j["progress"]["max"] > 1), None))
    assert job["progress"]["stage"] == "Base image"
    assert 0 < job["progress"]["overall"] < 1
    # the base pass alone can never fill the bar: it is 1 of 3.8 weight units
    assert job["progress"]["overall"] <= 1.0 / 3.8 + 1e-6


# --- tuned against measured runs, not node defaults --------------------------
def test_facedetailer_skips_a_face_that_is_already_large_enough():
    """Impact Pack's force_inpaint=True falls back to sampling the *entire*
    crop whenever the computed upscale is <= 1 -- i.e. whenever the face is
    already bigger than guide_size. On a portrait that re-renders the whole
    image for no resolution gain; it cost ~5 minutes of a 12-minute run.
    False makes it skip, which is what a detail pass is for."""
    for key in ("enhance-sdxl", "txt2img-sdxl-hq"):
        t = load_registry(REPO / "workflows").require(key).template
        assert t["27"]["inputs"]["force_inpaint"] is False


def test_the_hires_pass_is_not_oversampled(client):
    """The refine pass runs at denoise ~0.35, so it only traverses a third of
    the schedule. 25 steps across that stretch is far finer than the result
    justifies, and it is the most expensive stage in the chain."""
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    for key in ("enhance-sdxl", "txt2img-sdxl-hq"):
        assert ws[key]["defaults"]["refine_steps"] == 14
        assert ws[key]["defaults"]["refine"] <= 0.4
