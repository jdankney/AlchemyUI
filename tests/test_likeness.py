"""Phase 4: likeness from reference faces.

The spec names two failure modes it has already hit in practice. Both are
prevented here rather than handled later:

  1. InsightFace kills the whole batch if any one image has no detectable
     face -- so every reference is checked on its own, on upload.
  2. ImageBatch rescales and centre-crops later images to match the first --
     so every reference leaves normalisation at an identical square size.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from alchemyui.config import get_settings
from alchemyui.faces import FaceRejected, normalise_reference
from alchemyui.registry import RegistryError, load_registry, validate_entry
from tests.conftest import REPO

WORKFLOWS = REPO / "workflows"


def _reg():
    return load_registry(WORKFLOWS)


# --- graph shaping: the multi-reference chain -------------------------------
@pytest.mark.parametrize("n,expect_source", [
    (1, ["40", 0]), (2, ["50", 0]), (3, ["51", 0]), (4, ["52", 0]),
])
def test_reference_chain_is_pruned_to_the_number_supplied(n, expect_source):
    """The template carries all four slots so the boot validator can check
    them; a job keeps only as many as it was given."""
    w = _reg().require("likeness-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "references": [f"r{i}.png" for i in range(n)]}))
    assert g["32"]["inputs"]["image"] == expect_source

    live_slots = [s for s in w.references.slots if s in g]
    live_batch = [b for b in w.references.batch if b in g]
    assert len(live_slots) == n
    assert len(live_batch) == max(0, n - 1)
    # nothing dangling: every reference still points at a node that exists
    for node in g.values():
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in g, f"dangling link to {value[0]}"


def test_references_land_in_the_slots_in_order():
    w = _reg().require("likeness-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "references": ["a.png", "b.png", "c.png"]}))
    assert [g[s]["inputs"]["image"] for s in ("40", "41", "42")] == ["a.png", "b.png", "c.png"]


def test_more_references_than_slots_are_truncated_not_crashed():
    w = _reg().require("likeness-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "references": [f"r{i}.png" for i in range(9)]}))
    assert g["32"]["inputs"]["image"] == ["52", 0]


def test_no_references_is_an_error_not_a_broken_graph():
    w = _reg().require("likeness-sdxl")
    with pytest.raises(ValueError, match="at least one reference"):
        w.build_graph(w.resolve({"prompt": "x", "references": []}))


# --- the optional personal LoRA ---------------------------------------------
def test_lora_node_is_bypassed_when_none_is_chosen():
    """Not left at zero strength: the only LoRAs on disk are FaceID's own,
    which the unified loader already applies, and loading one twice has it
    fighting itself."""
    w = _reg().require("likeness-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "references": ["a.png"]}))
    assert "30" not in g
    # its consumers fall back to the checkpoint, not to a dead node
    assert g["6"]["inputs"]["clip"] == ["4", 1]
    assert g["7"]["inputs"]["clip"] == ["4", 1]
    assert g["31"]["inputs"]["model"] == ["4", 0]


def test_lora_node_stays_and_is_wired_when_one_is_chosen():
    w = _reg().require("likeness-sdxl")
    g = w.build_graph(w.resolve({
        "prompt": "x", "references": ["a.png"],
        "lora_name": "mine.safetensors", "lora_strength": 0.7}))
    assert g["30"]["inputs"]["lora_name"] == "mine.safetensors"
    assert g["30"]["inputs"]["strength_model"] == 0.7
    assert g["30"]["inputs"]["model"] == ["4", 0]
    assert g["6"]["inputs"]["clip"] == ["30", 1]
    assert g["31"]["inputs"]["model"] == ["30", 0]


# --- the template's own settings --------------------------------------------
def test_insightface_runs_on_the_cpu():
    """onnxruntime-gpu is not installed, and the GPU belongs to the sampler."""
    t = _reg().require("likeness-sdxl").template
    assert t["31"]["inputs"]["provider"] == "CPU"
    assert t["31"]["inputs"]["preset"] == "FACEID PLUS V2"


def test_both_faceid_weights_are_exposed():
    """They trade off differently, which is why the spec asks for two sliders."""
    w = _reg().require("likeness-sdxl")
    assert {"face_weight", "face_weight_v2"} <= set(w.params)
    assert w.params["face_weight"].field == "weight"
    assert w.params["face_weight_v2"].field == "weight_faceidv2"


# --- boot-time validation of the new machinery ------------------------------
def _entry(**over):
    e = {
        "template": "sdxl_faceid.v1.api.json", "display": "T",
        "params": {"prompt": {"node": "6", "field": "text"}}, "output_node": "9",
    }
    e.update(over)
    return e


def test_a_miswired_batch_chain_is_caught_at_boot():
    """Pruning assumes the chain really is a chain. If it is not, a job would
    silently drop the earlier references."""
    with pytest.raises(RegistryError, match="should take image1 from"):
        validate_entry("bad", _entry(references={
            "slots": ["40", "41", "42", "43"],
            "batch": ["51", "50", "52"],          # out of order
            "target": {"node": "32", "field": "image"},
        }), WORKFLOWS)


def test_wrong_number_of_batch_nodes_is_caught():
    with pytest.raises(RegistryError, match="batch nodes"):
        validate_entry("bad", _entry(references={
            "slots": ["40", "41", "42", "43"], "batch": ["50"],
            "target": {"node": "32", "field": "image"},
        }), WORKFLOWS)


def test_bypass_falling_back_to_a_nonexistent_input_is_caught():
    with pytest.raises(RegistryError, match="does not have"):
        validate_entry("bad", _entry(optional_nodes={
            "lora": {"node": "30", "outputs": {"0": "not_an_input"}, "enabled_by": "lora_name"},
        }), WORKFLOWS)


# --- face validation, the part that stops a batch dying ---------------------
def _face_png(path):
    return path.read_bytes()


def test_a_picture_with_no_face_is_refused_by_name():
    s = get_settings()

    class NoFaces:
        def detect(self, img):
            return []

    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (10, 120, 60)).save(buf, format="PNG")
    with pytest.raises(FaceRejected, match="No face found in meadow.jpg"):
        normalise_reference(buf.getvalue(), s, NoFaces(), "meadow.jpg")


def test_a_tiny_face_is_refused_with_the_number():
    from alchemyui.faces import DetectedFace

    s = get_settings()

    class TinyFace:
        def detect(self, img):
            return [DetectedFace((10, 10, 30, 30), 0.9)]

    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (90, 90, 90)).save(buf, format="PNG")
    with pytest.raises(FaceRejected, match="20px across"):
        normalise_reference(buf.getvalue(), s, TinyFace(), "far-away.jpg")


def test_every_reference_comes_out_the_same_square_size():
    """This is what leaves ImageBatch with nothing to rescale or crop."""
    from alchemyui.faces import DetectedFace

    s = get_settings()

    class Centre:
        def __init__(self, box):
            self.box = box

        def detect(self, img):
            return [DetectedFace(self.box, 0.9)]

    sizes = []
    for dims, box in [((1200, 800), (500, 300, 700, 560)),
                      ((640, 1400), (200, 900, 420, 1180)),   # face low in a tall frame
                      ((900, 900), (60, 60, 300, 340))]:      # face in the corner
        buf = io.BytesIO()
        Image.new("RGB", dims, (120, 100, 80)).save(buf, format="PNG")
        png, meta = normalise_reference(buf.getvalue(), s, Centre(box), "ref.png")
        sizes.append(Image.open(io.BytesIO(png)).size)
        assert meta["width"] == meta["height"] == s.reference_size
    assert len(set(sizes)) == 1, f"references differ in size: {sizes}"


def test_the_largest_face_is_used_and_the_count_reported():
    """A bystander in the background should not cost someone their upload."""
    from alchemyui.faces import DetectedFace

    s = get_settings()

    class Two:
        def detect(self, img):
            return [DetectedFace((400, 300, 700, 660), 0.9),   # the subject
                    DetectedFace((60, 60, 110, 115), 0.8)]     # someone behind

    buf = io.BytesIO()
    Image.new("RGB", (1000, 1000), (70, 70, 70)).save(buf, format="PNG")
    _, meta = normalise_reference(buf.getvalue(), s, Two(), "group.jpg")
    assert meta["faces_found"] == 2
    assert meta["face_px"] == 360         # the larger box, not the smaller


# --- HTTP ---------------------------------------------------------------------
def test_likeness_without_references_is_refused_before_the_gpu(client):
    r = client.post("/api/generate", json={"workflow_id": "likeness-sdxl", "prompt": "x"})
    assert r.status_code == 400
    assert "reference" in r.json()["detail"]
    assert not client.comfy.submitted


def test_an_unknown_reference_id_is_refused(client):
    r = client.post("/api/generate", json={
        "workflow_id": "likeness-sdxl", "prompt": "x", "reference_ids": ["nope"]})
    assert r.status_code == 404


def test_faceid_lora_is_kept_out_of_the_personal_picker(client):
    """IPAdapterUnifiedLoaderFaceID applies the matching one itself; offering
    it again would have it fighting its own weights."""
    names = client.get("/api/loras").json()["loras"]
    assert not any("faceid" in n.lower() for n in names)
