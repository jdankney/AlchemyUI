"""Phase 5: the platform layer.

Model picker across families, checkpoint-swap-aware queueing, batch
generation, favourites and tags, prompt search, presets, and the live preview
channel.
"""

from __future__ import annotations

import struct

from alchemyui.comfy import _decode_preview
from alchemyui.registry import load_registry
from tests.conftest import REPO
from tests.conftest import wait_for as _wait


# --- the model picker --------------------------------------------------------
def test_checkpoints_are_offered_per_family(client):
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    plain = {c["name"] for c in ws["txt2img-sdxl"]["checkpoints"]}
    assert len(plain) == 3                       # both SD1.5 models and SDXL
    # The chain and FaceID are tuned for SDXL, so nothing else is offered
    assert {c["family"] for c in ws["likeness-sdxl"]["checkpoints"]} == {"sdxl"}
    assert {c["family"] for c in ws["enhance-sdxl"]["checkpoints"]} == {"sdxl"}


def test_resolution_and_sampling_follow_the_model():
    """SD1.5 falls apart at SDXL's resolutions, so those settings belong to the
    checkpoint rather than to the workflow."""
    reg = load_registry(REPO / "workflows")
    w = reg.require("txt2img-sdxl")
    sdxl = reg.checkpoint("RealVisXL_V5.0_fp16.safetensors")
    sd15 = reg.checkpoint("epicrealism_naturalSinRC1VAE.safetensors")

    a = w.resolve({"prompt": "x", "aspect": "16:9"}, sdxl)
    b = w.resolve({"prompt": "x", "aspect": "16:9"}, sd15)
    assert (a["width"], a["height"]) == (1344, 768)
    assert (b["width"], b["height"]) == (896, 512)
    assert b["cfg"] != a["cfg"]                  # each model's own default
    assert b["checkpoint"] == sd15.name
    for p in (a, b):
        assert p["width"] % 8 == 0 and p["height"] % 8 == 0


def test_picking_a_model_reaches_the_graph(client):
    r = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "x",
        "checkpoint": "epicrealism_naturalSinRC1VAE.safetensors", "aspect": "1:1"})
    assert r.status_code == 200
    assert r.json()["params"]["width"] == 512
    _wait(client, lambda: client.comfy.submitted)
    g = client.comfy.submitted[0][1]
    assert g["4"]["inputs"]["ckpt_name"] == "epicrealism_naturalSinRC1VAE.safetensors"
    assert g["5"]["inputs"]["width"] == 512


def test_a_model_the_workflow_cannot_use_is_refused(client):
    """Better a clear refusal than a graph handed weights it was not built for."""
    r = client.post("/api/generate", json={
        "workflow_id": "likeness-sdxl", "prompt": "x", "reference_ids": [],
        "checkpoint": "epicrealism_naturalSinRC1VAE.safetensors"})
    assert r.status_code == 400

    r2 = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "x", "checkpoint": "not-a-model.safetensors"})
    assert r2.status_code == 400
    assert "unknown model" in r2.json()["detail"]


def test_timings_are_reported_per_model(client):
    """SD1.5 is roughly four times faster; one estimate across both would be
    wrong for each."""
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    t = ws["txt2img-sdxl"]["timings"]
    sdxl = t["RealVisXL_V5.0_fp16.safetensors"]
    sd15 = t["epicrealism_naturalSinRC1VAE.safetensors"]
    assert sd15["seconds_per_step"] < sdxl["seconds_per_step"]
    assert sd15["est_seconds"] < sdxl["est_seconds"]


# --- batch generation --------------------------------------------------------
def test_batch_makes_separate_jobs_with_different_seeds(client):
    """Separate jobs, not one batched graph: they queue, report and cancel
    individually, and a 6GB card will not hold a batched SDXL latent."""
    r = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "a heron", "batch": 4})
    body = r.json()
    assert len(body["job_ids"]) == 4 and body["batch"] == 4

    _wait(client, lambda: len(client.comfy.submitted) >= 4)
    seeds = [g["3"]["inputs"]["seed"] for _, g in client.comfy.submitted[:4]]
    assert len(set(seeds)) == 4, "a batch of identical seeds is four copies"
    assert len({pid for pid, _ in client.comfy.submitted[:4]}) == 4


def test_an_explicit_seed_still_varies_across_a_batch(client):
    client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "x", "seed": 100, "batch": 3})
    _wait(client, lambda: len(client.comfy.submitted) >= 3)
    seeds = [g["3"]["inputs"]["seed"] for _, g in client.comfy.submitted[:3]]
    assert seeds == [100, 101, 102]


def test_batch_is_capped(client):
    body = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "x", "batch": 99}).json()
    assert len(body["job_ids"]) == 8


# --- favourites and tags -----------------------------------------------------
def _one_image(client):
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "a fox"})
    return _wait(client, lambda: client.get("/api/images").json()["images"])[0]


def test_favourite_toggles_and_filters(client):
    img = _one_image(client)
    assert img["favorite"] is False
    assert client.patch(f"/api/images/{img['id']}", json={"favorite": True}).json()["favorite"]
    assert len(client.get("/api/images?favorite=true").json()["images"]) == 1
    client.patch(f"/api/images/{img['id']}", json={"favorite": False})
    assert client.get("/api/images?favorite=true").json()["images"] == []


def test_tags_are_normalised_deduplicated_and_filterable(client):
    img = _one_image(client)
    r = client.patch(f"/api/images/{img['id']}",
                     json={"tags": ["  Portrait ", "portrait", "KEEP", ""]})
    assert r.json()["tags"] == ["portrait", "keep"]      # lowercased, deduped, blanks dropped

    assert len(client.get("/api/images?tag=portrait").json()["images"]) == 1
    assert client.get("/api/images?tag=nope").json()["images"] == []
    # a tag filter must not match a substring of another tag
    assert client.get("/api/images?tag=port").json()["images"] == []

    tags = {t["tag"]: t["count"] for t in client.get("/api/tags").json()["tags"]}
    assert tags == {"portrait": 1, "keep": 1}


# --- search and history ------------------------------------------------------
def test_gallery_search_matches_the_prompt(client):
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "a red bicycle"})
    _wait(client, lambda: client.get("/api/images").json()["images"])
    client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "a blue whale"})
    _wait(client, lambda: len(client.get("/api/images").json()["images"]) == 2)

    assert len(client.get("/api/images?q=bicycle").json()["images"]) == 1
    assert len(client.get("/api/images?q=BICYCLE").json()["images"]) == 1   # case-insensitive
    assert client.get("/api/images?q=tractor").json()["images"] == []


def test_history_comes_back_with_the_settings_that_made_it(client):
    """Read out of the images table: storing the full parameter set on every
    image is what makes it the history and the reproducibility trail at once."""
    client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "a lighthouse at dusk", "seed": 7})
    _wait(client, lambda: client.get("/api/images").json()["images"])

    hist = client.get("/api/history").json()["history"]
    assert hist[0]["prompt"] == "a lighthouse at dusk"
    assert hist[0]["params"]["seed"] == 7            # enough to reproduce it
    assert client.get("/api/history?q=lighthouse").json()["history"]
    assert client.get("/api/history?q=zebra").json()["history"] == []


# --- presets -----------------------------------------------------------------
def test_presets_round_trip_and_saving_over_a_name_updates(client):
    body = {"name": "  My   Look ", "workflow_id": "txt2img-sdxl",
            "params": {"prompt": "x", "cfg": 9.0}}
    first = client.post("/api/presets", json=body).json()
    assert first["name"] == "My Look"                # whitespace collapsed

    body["params"]["cfg"] = 4.0
    again = client.post("/api/presets", json=body).json()
    assert again["id"] == first["id"]                # updated, not duplicated
    assert again["params"]["cfg"] == 4.0
    assert len(client.get("/api/presets").json()["presets"]) == 1

    assert client.delete(f"/api/presets/{first['id']}").status_code == 200
    assert client.get("/api/presets").json()["presets"] == []


def test_a_preset_for_an_unknown_workflow_is_refused(client):
    r = client.post("/api/presets", json={"name": "x", "workflow_id": "nope", "params": {}})
    assert r.status_code == 400


# --- live previews -----------------------------------------------------------
def test_preview_frames_are_decoded_from_the_binary_channel():
    """ComfyUI's envelope: a uint32 event type, then a uint32 image format,
    then the image."""
    jpeg = struct.pack(">I", 1) + struct.pack(">I", 1) + b"\xff\xd8\xff body"
    png = struct.pack(">I", 4) + struct.pack(">I", 2) + b"\x89PNG body"
    assert _decode_preview(jpeg)["media_type"] == "image/jpeg"
    assert _decode_preview(jpeg)["bytes"].startswith(b"\xff\xd8\xff")
    assert _decode_preview(png)["media_type"] == "image/png"

    assert _decode_preview(struct.pack(">I", 3) + b"text") is None   # not a preview
    assert _decode_preview(b"ab") is None                            # runt
    assert _decode_preview(struct.pack(">I", 1) + struct.pack(">I", 9) + b"x") is None


def test_preview_endpoint_is_404_until_a_frame_arrives(client):
    job_id = client.post("/api/generate", json={
        "workflow_id": "txt2img-sdxl", "prompt": "x"}).json()["job_id"]
    assert client.get(f"/api/jobs/{job_id}/preview").status_code == 404
    assert client.get("/api/jobs/nope/preview").status_code == 404
