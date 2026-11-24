"""Phase 8: region-aware editing.

The LLM chooses *what* to change and *where*; the app turns that into either
a CLIPSeg mask or a soft box mask, and feeds the existing inpainting UNet.
No GPU, no ComfyUI and no segmentation model are needed for these tests.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from alchemyui.planner.policy import NeedsInput, PolicyContext, compile_plan
from alchemyui.planner.schema import Plan, PlanInvalid, Vocabulary, validate_plan
from alchemyui.registry import load_registry
from alchemyui.storage import mask_from_box

REPO = Path(__file__).resolve().parent.parent
REG = load_registry(REPO / "workflows")
V = Vocabulary.from_registry(REG, 4)
RV6_INPAINT = "SD1.5-Inpaint/realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16.safetensors"


def raw(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse",
                negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                direction="", question="", options=[], region="", region_mode="", box=[])
    base.update(kw)
    return base


def plan(**kw):
    return Plan.from_dict(raw(**kw))


def ctx(size=(512, 512), likeness=True):
    return PolicyContext(None, size, likeness)


# --- what the model may say -------------------------------------------------
def test_a_text_region_is_a_segment_edit():
    p = validate_plan(json.dumps(raw(action="edit_region", source="a1",
                                     region="the sky")), V, ["a1"], [])
    assert p.action == "edit_region" and p.region == "the sky"
    assert p.region_mode == "segment" and p.box == ()


def test_a_box_is_kept_normalized_and_explicit():
    p = validate_plan(json.dumps(raw(action="edit_region", source="a1",
                                     region_mode="box", box=[100, 200, 800, 900])),
                      V, ["a1"], [])
    assert p.region_mode == "box" and p.box == (100, 200, 800, 900)
    assert p.region == ""


@pytest.mark.parametrize("over, why", [
    ({"source": "a1"}, "needs region"),
    ({"source": "a1", "region_mode": "box"}, "needs box"),
    ({"source": "a1", "region_mode": "box", "box": [1, 2, 3]}, "four"),
    ({"source": "a1", "region_mode": "box", "box": [-1, 2, 50, 60]}, "between 0 and 1000"),
    ({"source": "a1", "region_mode": "box", "box": [50, 50, 25, 60]}, "right/bottom"),
])
def test_an_incomplete_region_edit_says_why(over, why):
    with pytest.raises(PlanInvalid, match=why):
        validate_plan(json.dumps(raw(action="edit_region", **over)), V, ["a1"], [])


def test_a_person_can_be_named_as_the_face_reference():
    p = validate_plan(json.dumps(raw(action="edit_region", source="a2", faces="a1",
                                     region_mode="box", box=[200, 100, 800, 950])),
                      V, ["a1", "a2"], ["a1", "a2"])
    assert p.source == "a2" and p.faces == "a1"


# --- the policy --------------------------------------------------------------
def test_a_segment_edit_uses_the_inpainting_model_and_carries_the_phrase():
    c = compile_plan(plan(action="edit_region", source="a1", region="the sky"),
                     V, REG, ctx())
    assert c.workflow == "edit-region-sdxl"
    assert c.checkpoint == RV6_INPAINT
    assert c.raw["region"] == "the sky"
    assert c.chip == "Editing · the sky · Realistic Vision 6.0 Inpainting"


def test_a_box_edit_uses_the_box_workflow_and_no_segment_phrase():
    c = compile_plan(plan(action="edit_region", source="a1", region_mode="box",
                          box=(100, 100, 900, 900), prompt="a person on a bed"),
                     V, REG, ctx())
    assert c.workflow == "edit-region-box-sdxl"
    assert c.checkpoint == RV6_INPAINT
    assert "region" not in c.raw
    assert c.region_mode == "box" and c.box == (100, 100, 900, 900)
    assert c.chip == "Placing · Realistic Vision 6.0 Inpainting"


def test_a_region_edit_ignores_a_photoreal_pin_and_explains():
    c = compile_plan(plan(action="edit_region", source="a1", region="the sky"),
                     V, REG, PolicyContext(None, (512, 512), True,
                                           "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"))
    assert c.checkpoint == RV6_INPAINT
    assert "inpainting" in c.note


def test_me_without_a_saved_likeness_asks_before_a_region_edit():
    with pytest.raises(NeedsInput, match="saved a likeness"):
        compile_plan(plan(action="edit_region", source="a1", faces="me",
                          region_mode="box", box=(100, 100, 900, 900)),
                     V, REG, PolicyContext(None, (512, 512), False))


# --- the mask the app draws --------------------------------------------------
def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("L")


def test_a_box_becomes_a_soft_white_mask():
    png = mask_from_box(512, 512, (250, 100, 750, 900))
    im = _open(png)
    assert im.size == (512, 512)
    assert im.getpixel((256, 256)) == 255
    assert im.getpixel((30, 30)) == 0
    # the border is deliberately soft, not a hard rectangle
    assert any(0 < v < 255 for v in im.tobytes())


def test_a_tiny_or_backwards_box_is_refused():
    with pytest.raises(Exception, match="too small"):
        mask_from_box(512, 512, (100, 100, 104, 500))
    with pytest.raises(Exception, match="too small"):
        mask_from_box(512, 512, (500, 100, 100, 500))


# --- graph shaping -----------------------------------------------------------
def test_a_region_edit_without_a_face_drops_the_whole_faceid_chain():
    w = REG.require("edit-region-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "image": "a.png", "region": "the sky"}))
    assert "30" not in g and "31" not in g and "32" not in g
    assert "40" not in g and "50" not in g
    assert g["3"]["inputs"]["model"] == ["4", 0]
    assert g["6"]["inputs"]["clip"] == ["4", 1]


def test_a_face_reference_keeps_the_faceid_chain_and_prunes_unused_slots():
    w = REG.require("edit-region-box-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "image": "a.png", "mask": "m.png",
                                 "references": ["me.png"]}))
    assert g["32"]["inputs"]["image"] == ["40", 0]
    assert "41" not in g and "50" not in g
    assert g["3"]["inputs"]["model"] == ["32", 0]
    assert g["12"]["inputs"]["image"] == "m.png"


# --- the turn engine turns a plan into a job ---------------------------------
def P(**kw):
    return Plan.from_dict(raw(**kw))


async def settle(h, turn_id, timeout=5):
    t = None
    for _ in range(int(timeout / 0.02)):
        t = await h.engine.turn_public(await h.row(turn_id))
        if t["state"] in ("done", "failed", "needs_input", "cancelled"):
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn stuck in {t and t['state']}")


async def test_a_box_edit_makes_a_mask_and_queues_the_inpaint(engine_harness):
    from alchemyui.planner.router import RouteResult

    h = engine_harness
    up = await h.upload(h.alice, (256, 256))
    h.router.results = [RouteResult(P(action="edit_region", source="a1",
                                      region_mode="box", box=(100, 100, 900, 900),
                                      prompt="a person lying on a bed"), "primary")]
    t = await h.engine.create_turn(h.alice, "add me to my bed", [up])
    done = await settle(h, t["id"])
    assert done["state"] == "done"
    job = done["jobs"][0]
    assert job["workflow_id"] == "edit-region-box-sdxl"
    assert job["params"]["mask"].startswith("alchemyui/")
    assert any(name.endswith(".mask.png") for name, _ in h.comfy.uploaded)


async def test_a_segment_edit_passes_the_region_to_the_graph(engine_harness):
    from alchemyui.planner.router import RouteResult

    h = engine_harness
    up = await h.upload(h.alice, (256, 256))
    h.router.results = [RouteResult(P(action="edit_region", source="a1",
                                      region="the sky", prompt="a sunset sky"), "primary")]
    t = await h.engine.create_turn(h.alice, "change the sky", [up])
    done = await settle(h, t["id"])
    assert done["state"] == "done"
    job = done["jobs"][0]
    assert job["workflow_id"] == "edit-region-sdxl"
    assert job["params"]["region"] == "the sky"
