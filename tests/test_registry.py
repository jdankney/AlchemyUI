"""The registry is the one place a workflow can silently rot: node IDs change
whenever a graph is rebuilt in the ComfyUI editor. These check that a drifted
binding fails loudly, by name, at load time -- never mid-generation.

Everything here is pure JSON: no GPU, no ComfyUI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alchemyui.registry import RegistryError, load_registry, validate_entry

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / "workflows"


def _entry(**over) -> dict:
    entry = {
        "template": "sdxl_txt2img.v1.api.json",
        "display": "Test",
        "checkpoint": "x.safetensors",
        "params": {"prompt": {"node": "6", "field": "text"}},
        "output_node": "9",
    }
    entry.update(over)
    return entry


# --- the shipped registry ---------------------------------------------------
def test_shipped_registry_is_valid():
    reg = load_registry(WORKFLOWS)
    w = reg.require("txt2img-sdxl")
    assert w.progress_node == "3"
    assert w.output_node == "9"
    assert w.template[w.progress_node]["class_type"] == "KSampler"
    assert w.template[w.output_node]["class_type"] == "SaveImage"


def test_every_bound_node_and_field_exists_in_the_template():
    for w in load_registry(WORKFLOWS).all():
        for name, b in w.params.items():
            assert b.node in w.template, f"{w.key}.{name} -> missing node {b.node}"
            assert b.field in w.template[b.node]["inputs"], f"{w.key}.{name} -> missing field"


# --- drift ------------------------------------------------------------------
def test_unknown_node_id_is_rejected_by_name():
    with pytest.raises(RegistryError) as e:
        validate_entry("bad", _entry(params={"prompt": {"node": "99", "field": "text"}}), WORKFLOWS)
    assert "bad" in str(e.value) and "99" in str(e.value)


def test_unknown_field_is_rejected_and_lists_what_exists():
    with pytest.raises(RegistryError) as e:
        validate_entry("bad", _entry(params={"prompt": {"node": "6", "field": "txet"}}), WORKFLOWS)
    msg = str(e.value)
    assert "txet" in msg
    assert "text" in msg          # names the fields that are actually there


def test_missing_output_node_is_rejected():
    with pytest.raises(RegistryError, match="output_node"):
        validate_entry("bad", _entry(output_node="42"), WORKFLOWS)


def test_missing_progress_node_is_rejected():
    with pytest.raises(RegistryError, match="progress_node"):
        validate_entry("bad", _entry(progress_node="42"), WORKFLOWS)


def test_missing_template_file_is_rejected():
    with pytest.raises(RegistryError, match="template not found"):
        validate_entry("bad", _entry(template="nope.json"), WORKFLOWS)


def test_width_binding_without_aspects_is_rejected():
    with pytest.raises(RegistryError, match="aspects"):
        validate_entry("bad", _entry(params={"width": {"node": "5", "field": "width"}}), WORKFLOWS)


# --- the most likely mistake of all -----------------------------------------
def test_ui_format_export_is_named_as_such(tmp_path):
    """The regular Save writes a UI graph, which /prompt rejects at validation.
    That mistake deserves its own message, not a generic parse error."""
    (tmp_path / "ui.json").write_text(json.dumps({
        "last_node_id": 9, "nodes": [{"id": 3, "type": "KSampler"}], "links": [[1, 4, 0, 3, 0]],
    }))
    with pytest.raises(RegistryError) as e:
        validate_entry("bad", _entry(template="ui.json"), tmp_path)
    assert "API format" in str(e.value)


def test_non_api_node_shape_is_rejected(tmp_path):
    (tmp_path / "odd.json").write_text(json.dumps({"3": {"class_type": "KSampler"}}))
    with pytest.raises(RegistryError, match="not API format"):
        validate_entry("bad", _entry(template="odd.json"), tmp_path)


# --- parameter resolution ---------------------------------------------------
def test_resolve_clamps_and_fills_defaults():
    w = load_registry(WORKFLOWS).require("txt2img-sdxl")
    p = w.resolve({"prompt": " a cat ", "steps": 5000, "cfg": -3, "aspect": "9:16"})
    assert p["prompt"] == "a cat"
    assert p["steps"] == int(w.limits["steps"][1])
    assert p["cfg"] == w.limits["cfg"][0]
    assert (p["width"], p["height"]) == (768, 1344)
    assert p["negative"] == w.defaults["negative"]     # untouched -> registry default
    assert p["sampler"] == w.defaults["sampler"]
    assert p["checkpoint"] == w.checkpoint


def test_resolve_rejects_a_sampler_comfyui_would_not_accept():
    w = load_registry(WORKFLOWS).require("txt2img-sdxl")
    assert w.resolve({"prompt": "x", "sampler": "not_a_sampler"})["sampler"] == w.defaults["sampler"]


def test_resolve_keeps_seed_zero_but_invents_one_when_absent():
    w = load_registry(WORKFLOWS).require("txt2img-sdxl")
    assert w.resolve({"prompt": "x", "seed": 0})["seed"] == 0      # 0 is a real choice
    a = w.resolve({"prompt": "x"})["seed"]
    b = w.resolve({"prompt": "x"})["seed"]
    assert a != b                                                   # absent -> random


def test_build_graph_injects_without_touching_the_template():
    w = load_registry(WORKFLOWS).require("txt2img-sdxl")
    before = json.dumps(w.template, sort_keys=True)
    g = w.build_graph(w.resolve({"prompt": "hello", "aspect": "16:9", "seed": 7}))
    assert g["6"]["inputs"]["text"] == "hello"
    assert g["3"]["inputs"]["seed"] == 7
    assert g["5"]["inputs"]["width"] == 1344
    assert g["3"]["inputs"]["model"] == ["4", 0]          # wiring survives the clone
    assert json.dumps(w.template, sort_keys=True) == before


# --- Phase 2 entries --------------------------------------------------------
def test_editing_workflows_bind_what_they_claim_to_require():
    reg = load_registry(WORKFLOWS)
    for key, kind, needs in [("img2img-sdxl", "img2img", ("image",)),
                             ("inpaint-sdxl", "inpaint", ("image", "mask")),
                             ("outpaint-sdxl", "outpaint", ("image",))]:
        w = reg.require(key)
        assert w.kind == kind
        assert w.requires == needs
        for name in needs:
            assert name in w.params, f"{key} requires {name} but binds no such param"


def test_requires_without_a_binding_is_rejected():
    """Otherwise the UI collects an upload that never reaches the graph."""
    with pytest.raises(RegistryError, match="nothing in the graph consumes it"):
        validate_entry("bad", _entry(requires=["image"]), WORKFLOWS)


def test_unknown_kind_is_rejected():
    with pytest.raises(RegistryError, match="unknown kind"):
        validate_entry("bad", _entry(kind="wishful-thinking"), WORKFLOWS)


def test_inpaint_reads_the_mask_from_the_red_channel():
    """LoadImageMask's "alpha" channel returns 1.0 - alpha. Using it would
    invert the mask and repaint everything except what was brushed."""
    w = load_registry(WORKFLOWS).require("inpaint-sdxl")
    assert w.template["12"]["class_type"] == "LoadImageMask"
    assert w.template["12"]["inputs"]["channel"] == "red"


def test_img2img_has_no_empty_latent_so_it_keeps_the_source_size():
    w = load_registry(WORKFLOWS).require("img2img-sdxl")
    assert not any(n["class_type"] == "EmptyLatentImage" for n in w.template.values())
    assert "width" not in w.params and "height" not in w.params


def test_resolve_skips_aspect_for_workflows_that_do_not_size_their_own_canvas():
    reg = load_registry(WORKFLOWS)
    p = reg.require("img2img-sdxl").resolve({"prompt": "x", "aspect": "16:9"})
    assert "width" not in p and "aspect" not in p
    assert reg.require("txt2img-sdxl").resolve({"prompt": "x"})["width"] == 1024


def test_resolve_carries_image_and_mask_references_through_untouched():
    w = load_registry(WORKFLOWS).require("inpaint-sdxl")
    p = w.resolve({"prompt": "x", "image": "alchemyui/a.png", "mask": "alchemyui/a.mask.png"})
    assert p["image"] == "alchemyui/a.png"
    assert p["mask"] == "alchemyui/a.mask.png"


def test_outpaint_pads_are_clamped_per_side():
    w = load_registry(WORKFLOWS).require("outpaint-sdxl")
    p = w.resolve({"prompt": "x", "pad_left": -50, "pad_right": 99999})
    assert p["pad_left"] == 0 and p["pad_right"] == 512
