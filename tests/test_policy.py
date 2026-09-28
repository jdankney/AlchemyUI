"""The model picks meaning; code picks numbers (spec section 7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemyui.planner.policy import NeedsInput, PolicyContext, compile_plan
from alchemyui.planner.schema import Plan, Vocabulary
from alchemyui.registry import load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
V = Vocabulary.from_registry(REG, 4)
RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
EPIC = "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"
RV6_INPAINT = "SD1.5-Inpaint/realisticVisionV60B1_v60B1InpaintingVAE_pruned_fp16.safetensors"


def plan(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="3:4", count=1, strength="medium", quality="fast", direction="",
                question="", options=())
    base.update(kw)
    return Plan(**base)


def ctx(resident=None, size=None, likeness=True):
    return PolicyContext(resident, size, likeness)


def test_fast_generation_is_rv6_and_best_is_sdxl():
    assert compile_plan(plan(), V, REG, ctx()).checkpoint == RV6
    assert compile_plan(plan(quality="best"), V, REG, ctx()).checkpoint == XL


def test_fast_prefers_whatever_fast_model_is_already_loaded():
    assert compile_plan(plan(), V, REG, ctx(resident=EPIC)).checkpoint == EPIC
    # a resident model without the trait is not a reason to use it
    assert compile_plan(plan(), V, REG, ctx(resident=XL)).checkpoint == RV6


def test_generate_carries_meaning_through_as_numbers():
    c = compile_plan(plan(count=3, negative="text"), V, REG, ctx())
    assert c.workflow == "txt2img-sdxl" and c.count == 3
    assert c.raw["prompt"] == "a lighthouse" and c.raw["aspect"] == "3:4"
    assert c.raw["negative"].startswith("text, ")      # the model's words, then the house list
    assert c.chip == "Generating 3 · Realistic Vision 6.0 · 3:4"


@pytest.mark.parametrize("word, denoise", [("subtle", 0.35), ("medium", 0.55), ("strong", 0.75)])
def test_strength_words_become_denoise(word, denoise):
    c = compile_plan(plan(action="transform", source="a1", strength=word), V, REG,
                     ctx(size=(1024, 1024)))
    assert c.workflow == "img2img-sdxl" and c.raw["denoise"] == denoise
    assert "aspect" not in c.raw                        # img2img keeps the source's shape


def test_a_small_source_stays_on_sd15_even_for_best():
    """SDXL is trained at 1024; handing it a 512px image makes mush."""
    c = compile_plan(plan(action="transform", source="t1.1", quality="best"), V, REG,
                     ctx(size=(512, 768)))
    assert REG.checkpoint(c.checkpoint).family == "sd15"


def test_a_large_source_may_use_sdxl_for_best():
    c = compile_plan(plan(action="transform", source="a1", quality="best"), V, REG,
                     ctx(size=(1024, 1024)))
    assert c.checkpoint == XL


def test_extend_wider_pads_both_sides_on_the_inpainting_unet():
    c = compile_plan(plan(action="extend", source="a1", direction="wider"), V, REG,
                     ctx(size=(512, 512)))
    assert c.checkpoint == RV6_INPAINT
    pads = tuple(c.raw[k] for k in ("pad_left", "pad_right", "pad_top", "pad_bottom"))
    assert pads == (128, 128, 0, 0)


@pytest.mark.parametrize("direction, nonzero", [
    ("left", {"pad_left"}), ("up", {"pad_top"}), ("taller", {"pad_top", "pad_bottom"}),
    ("all", {"pad_left", "pad_right", "pad_top", "pad_bottom"})])
def test_every_direction_pads_a_multiple_of_8(direction, nonzero):
    c = compile_plan(plan(action="extend", source="a1", direction=direction), V, REG,
                     ctx(size=(1000, 760)))
    got = {k for k in ("pad_left", "pad_right", "pad_top", "pad_bottom") if c.raw[k]}
    assert got == nonzero
    assert all(c.raw[k] % 8 == 0 and 64 <= c.raw[k] <= 512 for k in got)


def test_enhance_runs_on_the_one_family_it_supports():
    c = compile_plan(plan(action="enhance", source="a1"), V, REG, ctx(size=(1024, 1024)))
    assert c.workflow == "enhance-sdxl" and c.checkpoint == XL and c.count == 1


def test_likeness_without_a_saved_likeness_asks():
    with pytest.raises(NeedsInput, match="saved a likeness"):
        compile_plan(plan(action="likeness", faces="me"), V, REG, ctx(likeness=False))


def test_likeness_from_an_attachment_does_not_need_a_saved_one():
    c = compile_plan(plan(action="likeness", faces="a1"), V, REG, ctx(likeness=False))
    assert c.workflow == "likeness-sdxl"


def test_ask_and_select():
    with pytest.raises(NeedsInput) as e:
        compile_plan(plan(action="ask", question="Which one?", options=("1", "2")), V, REG, ctx())
    assert e.value.question == "Which one?"
    assert [o["label"] for o in e.value.options] == ["1", "2"]
    c = compile_plan(plan(action="select", source="t2.2"), V, REG, ctx())
    assert c.workflow is None and c.count == 0


# --- Phase 7.5: a pinned model -----------------------------------------------
def pinned(model, **kw):
    return PolicyContext(kw.get("resident"), kw.get("size"), kw.get("likeness", True), model)


def test_a_pin_replaces_the_trait_lookup_for_generate_and_transform():
    c = compile_plan(plan(), V, REG, pinned(EPIC))
    assert c.checkpoint == EPIC and c.pinned == EPIC and c.note == ""
    assert c.chip == "Generating · epiCRealism · 3:4"
    # the planner's quality word does not outrank a person's pick
    assert compile_plan(plan(quality="best"), V, REG, pinned(RV6)).checkpoint == RV6
    t = compile_plan(plan(action="transform", source="a1"), V, REG, pinned(XL, size=(1024, 1024)))
    assert t.checkpoint == XL and t.note == ""


def test_extending_always_uses_the_inpainting_model_and_says_so():
    c = compile_plan(plan(action="extend", source="a1", direction="left"), V, REG,
                     pinned(EPIC, size=(512, 512)))
    assert c.checkpoint == RV6_INPAINT
    assert "inpainting" in c.note and "epiCRealism" in c.note


def test_an_sdxl_only_workflow_ignores_an_sd15_pin_and_says_so():
    c = compile_plan(plan(action="likeness", faces="me"), V, REG, pinned(RV6))
    assert c.checkpoint == XL
    assert "SDXL" in c.note


def test_a_small_source_is_not_handed_to_sdxl_even_when_pinned():
    c = compile_plan(plan(action="transform", source="a1"), V, REG, pinned(XL, size=(512, 512)))
    assert c.checkpoint == RV6
    assert "too small" in c.note


def test_the_models_look_and_negative_are_composed_into_the_job():
    reg = load_registry(Path(__file__).resolve().parent.parent / "workflows")
    reg.set_override(EPIC, {"look": "RAW photo, film grain", "negative": "cgi, 3d render"})
    c = compile_plan(plan(negative="text"), V, reg, pinned(EPIC))
    assert c.raw["prompt"] == "a lighthouse, RAW photo, film grain"
    assert c.raw["negative"] == "text, cgi, 3d render"
    # unpinned, the policy's own pick composes with its own (untuned) profile
    assert compile_plan(plan(), V, reg, ctx()).raw["prompt"] == "a lighthouse"


def test_enhancing_a_small_image_with_an_sdxl_pin_needs_no_note():
    c = compile_plan(plan(action="enhance", source="a1"), V, REG, pinned(XL, size=(512, 512)))
    assert c.checkpoint == XL and c.note == ""
