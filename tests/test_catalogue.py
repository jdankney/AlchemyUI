"""The shipped model catalogue matches what is on disk: checkpoints named by
their family folder (as ComfyUI lists them), inpainting on dedicated
inpainting UNets, and fast few-step defaults for the Hyper variants."""

from __future__ import annotations

from pathlib import Path

from alchemyui.registry import load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
FOLDER = {"sdxl": "SDXL/", "sd15": "SD1.5/", "sd15-inpaint": "SD1.5-Inpaint/"}


def test_every_checkpoint_is_named_by_its_family_folder():
    for c in REG.all_checkpoints():
        assert c.name.startswith(FOLDER[c.family]), c.name


def test_every_checkpoint_has_known_traits():
    for c in REG.all_checkpoints():
        assert set(c.traits) <= {"fast", "quality", "photoreal", "inpaint", "draft"}, c.name
        assert ("inpaint" in c.traits) == (c.family == "sd15-inpaint"), c.name


def test_inpaint_and_outpaint_default_to_the_inpainting_unet():
    for key in ("inpaint-sdxl", "outpaint-sdxl"):
        w = REG.require(key)
        assert REG.checkpoint(w.checkpoint).family == "sd15-inpaint", key


def test_generation_cannot_pick_an_inpainting_unet():
    w = REG.require("txt2img-sdxl")
    assert all(c.family != "sd15-inpaint" for c in REG.checkpoints_for(w))


def test_hyper_checkpoints_sample_in_a_few_steps():
    hyper = [c for c in REG.all_checkpoints() if "Hyper" in c.name]
    assert len(hyper) == 2
    for c in hyper:
        assert c.defaults["steps"] <= 8 and c.defaults["cfg"] <= 2.5
        assert c.defaults["sampler"] == "dpmpp_sde"


def test_a_v1_bare_filename_still_resolves():
    c = REG.checkpoint("epicrealism_naturalSinRC1VAE.safetensors")
    assert c is not None and c.name == "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"
    w = REG.require("txt2img-sdxl")
    assert w.resolve({"prompt": "x"}, c)["checkpoint"] == c.name


def test_hyper_is_a_draft_model_and_rv6_is_the_fast_one():
    """Phase 6 measured Hyper watermarking its output; `fast` means RV 6.0."""
    for c in REG.all_checkpoints():
        if "Hyper" in c.name:
            assert "draft" in c.traits and "fast" not in c.traits, c.name
    fast = [c.name for c in REG.all_checkpoints() if "fast" in c.traits]
    assert fast[0] == "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
