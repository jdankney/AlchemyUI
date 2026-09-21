"""Phase 2 at the HTTP level: img2img, inpaint and outpaint.

The three are separate workflows with separate templates, so what is checked
here is mostly that each one's parameters reach the right node -- and that the
app refuses a job whose inputs are incomplete rather than submitting a graph
that would fail on the GPU.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from tests.conftest import wait_for as _wait


def _png(size=(640, 480), colour=(120, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _mask_png(size=(640, 480)) -> bytes:
    m = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(m).ellipse([100, 100, 300, 300], fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    m.save(buf, format="PNG")
    return buf.getvalue()


def _upload(client, data=None, name="src.png"):
    r = client.post("/api/uploads", files={"file": (name, data or _png(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()


def _attach_mask(client, upload_id, data=None):
    return client.post(f"/api/uploads/{upload_id}/mask",
                       files={"file": ("mask.png", data or _mask_png(), "image/png")})


# --- the registry ------------------------------------------------------------
def test_every_workflow_is_offered_with_its_kind(client):
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    assert {w["kind"] for w in ws.values()} == {
        "txt2img", "img2img", "inpaint", "outpaint", "enhance"}
    assert ws["img2img-sdxl"]["requires"] == ["image"]
    assert ws["inpaint-sdxl"]["requires"] == ["image", "mask"]
    assert ws["outpaint-sdxl"]["requires"] == ["image"]
    # the spec's denoise range, so the UI's plain-language labels line up
    assert ws["img2img-sdxl"]["limits"]["denoise"] == [0.3, 0.9]
    # img2img and inpaint take their size from the source image, not an aspect
    assert "aspect" not in ws["img2img-sdxl"]["inputs"]
    assert "aspect" in ws["txt2img-sdxl"]["inputs"]


# --- uploads -----------------------------------------------------------------
def test_upload_is_normalised_and_sent_to_comfyui(client):
    up = _upload(client, _png((1000, 750)))
    assert (up["width"], up["height"]) == (1000, 744)     # snapped to /8
    assert client.comfy.uploaded, "nothing reached ComfyUI"
    assert client.get(up["url"]).content.startswith(b"\x89PNG")


def test_mask_must_belong_to_a_real_upload(client):
    assert _attach_mask(client, "nope").status_code == 404


def test_an_unpainted_mask_is_refused(client):
    up = _upload(client)
    blank = Image.new("RGBA", (up["width"], up["height"]), (0, 0, 0, 0))
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    r = _attach_mask(client, up["id"], buf.getvalue())
    assert r.status_code == 400
    assert "painted" in r.json()["detail"]


# --- img2img -----------------------------------------------------------------
def test_img2img_sends_the_source_and_denoise_to_the_right_nodes(client):
    up = _upload(client)
    r = client.post("/api/generate", json={
        "workflow_id": "img2img-sdxl", "prompt": "make it autumn",
        "image_id": up["id"], "denoise": 0.42, "seed": 5,
    })
    assert r.status_code == 200
    _wait(client, lambda: client.comfy.submitted)
    _, graph = client.comfy.submitted[0]
    assert graph["10"]["class_type"] == "LoadImage"
    assert graph["10"]["inputs"]["image"].endswith(".png")
    assert graph["11"]["class_type"] == "VAEEncode"          # not ForInpaint
    assert graph["3"]["inputs"]["denoise"] == 0.42
    assert graph["3"]["inputs"]["latent_image"] == ["11", 0]  # the encoded source, not an empty latent
    assert "5" not in graph                                   # no EmptyLatentImage at all


def test_denoise_is_clamped_to_the_useful_range(client):
    up = _upload(client)
    for sent, expected in ((0.05, 0.3), (5.0, 0.9)):
        r = client.post("/api/generate", json={
            "workflow_id": "img2img-sdxl", "prompt": "x",
            "image_id": up["id"], "denoise": sent})
        assert r.json()["params"]["denoise"] == expected


def test_img2img_without_an_image_is_refused(client):
    r = client.post("/api/generate", json={"workflow_id": "img2img-sdxl", "prompt": "x"})
    assert r.status_code == 400
    assert "image" in r.json()["detail"]


# --- inpaint -----------------------------------------------------------------
def test_inpaint_wires_source_and_mask_into_the_inpaint_encoder(client):
    up = _upload(client)
    assert _attach_mask(client, up["id"]).status_code == 200
    r = client.post("/api/generate", json={
        "workflow_id": "inpaint-sdxl", "prompt": "a stone well", "image_id": up["id"]})
    assert r.status_code == 200

    _wait(client, lambda: client.comfy.submitted)
    _, graph = client.comfy.submitted[0]
    assert graph["11"]["class_type"] == "VAEEncodeForInpaint"
    assert graph["11"]["inputs"]["mask"] == ["12", 0]
    assert graph["12"]["class_type"] == "LoadImageMask"
    # "red", not "alpha": alpha would hand back 1.0 - alpha and invert the mask
    assert graph["12"]["inputs"]["channel"] == "red"
    assert graph["10"]["inputs"]["image"] != graph["12"]["inputs"]["image"]
    # the region is blanked by the encoder, so there is nothing to preserve
    assert graph["3"]["inputs"]["denoise"] == 1.0


def test_inpaint_without_a_mask_is_refused_before_it_reaches_the_gpu(client):
    up = _upload(client)
    r = client.post("/api/generate", json={
        "workflow_id": "inpaint-sdxl", "prompt": "x", "image_id": up["id"]})
    assert r.status_code == 400
    assert "brush" in r.json()["detail"].lower()
    assert not client.comfy.submitted


def test_grow_mask_reaches_the_encoder(client):
    up = _upload(client)
    _attach_mask(client, up["id"])
    client.post("/api/generate", json={
        "workflow_id": "inpaint-sdxl", "prompt": "x", "image_id": up["id"], "grow_mask": 24})
    _wait(client, lambda: client.comfy.submitted)
    assert client.comfy.submitted[0][1]["11"]["inputs"]["grow_mask_by"] == 24


def test_grow_mask_defaults_above_comfyuis_own(client):
    """ComfyUI defaults grow_mask_by to 6, which leaves a visible ring with a
    non-inpainting checkpoint. Measured on real output, 32 is much cleaner."""
    ws = {w["id"]: w for w in client.get("/api/workflows").json()["workflows"]}
    assert ws["inpaint-sdxl"]["defaults"]["grow_mask"] == 32


# --- outpaint ----------------------------------------------------------------
def test_outpaint_pads_reach_the_pad_node_and_its_mask_is_used(client):
    up = _upload(client)
    client.post("/api/generate", json={
        "workflow_id": "outpaint-sdxl", "prompt": "more field",
        "image_id": up["id"], "pad_left": 128, "pad_right": 256,
        "pad_top": 0, "pad_bottom": 64, "feathering": 20})
    _wait(client, lambda: client.comfy.submitted)
    _, graph = client.comfy.submitted[0]
    pad = graph["13"]
    assert pad["class_type"] == "ImagePadForOutpaint"
    assert (pad["inputs"]["left"], pad["inputs"]["right"]) == (128, 256)
    assert (pad["inputs"]["top"], pad["inputs"]["bottom"]) == (0, 64)
    assert pad["inputs"]["feathering"] == 20
    # the pad node's own MASK output (index 1) is what gets generated into
    assert graph["11"]["inputs"]["pixels"] == ["13", 0]
    assert graph["11"]["inputs"]["mask"] == ["13", 1]


def test_outpaint_with_no_direction_is_refused(client):
    up = _upload(client)
    r = client.post("/api/generate", json={
        "workflow_id": "outpaint-sdxl", "prompt": "x", "image_id": up["id"],
        "pad_left": 0, "pad_top": 0, "pad_right": 0, "pad_bottom": 0})
    assert r.status_code == 400
    assert "direction" in r.json()["detail"]


def test_pads_are_capped_so_a_6gb_card_survives(client):
    up = _upload(client)
    r = client.post("/api/generate", json={
        "workflow_id": "outpaint-sdxl", "prompt": "x", "image_id": up["id"],
        "pad_right": 99999})
    assert r.json()["params"]["pad_right"] == 512


# --- provenance --------------------------------------------------------------
def test_the_job_records_which_image_and_mask_it_used(client):
    up = _upload(client)
    _attach_mask(client, up["id"])
    job_id = client.post("/api/generate", json={
        "workflow_id": "inpaint-sdxl", "prompt": "x", "image_id": up["id"]}).json()["job_id"]
    row = client.db._conn.execute(
        "SELECT input_image_path, mask_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row[0] and row[1], "the job should point at its source and mask"


def test_an_edited_image_records_the_settings_that_made_it(client):
    up = _upload(client)
    client.post("/api/generate", json={
        "workflow_id": "img2img-sdxl", "prompt": "autumn",
        "image_id": up["id"], "denoise": 0.6})
    img = _wait(client, lambda: client.get("/api/images").json()["images"])[0]
    assert img["params"]["denoise"] == 0.6
    assert img["params"]["workflow_id"] == "img2img-sdxl"
    assert img["params"]["image"]          # the ComfyUI source filename, for the trail
