"""Phase 9: the with_reference plan, its policy, graph shaping and the
search -> pick -> fetch -> queue path through the turn engine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from alchemyui.planner.policy import NeedsInput, PolicyContext, compile_plan
from alchemyui.planner.schema import Plan, PlanInvalid, Vocabulary, validate_plan
from alchemyui.registry import load_registry

REPO = Path(__file__).resolve().parent.parent
REG = load_registry(REPO / "workflows")
V = Vocabulary.from_registry(REG, 4)
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"
RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"


def raw(**kw):
    base = dict(action="generate", source="", faces="", prompt="a person",
                negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                direction="", question="", options=[], region="", region_mode="", box=[],
                search="", reference="")
    base.update(kw)
    return base


def plan(**kw):
    return Plan.from_dict(raw(**kw))


def ctx(size=None, likeness=True):
    return PolicyContext(None, size, likeness)


# --- what the model may say -------------------------------------------------
def test_with_reference_needs_a_query_and_something_to_put_it_on():
    p = validate_plan(json.dumps(raw(action="with_reference", faces="me",
                                     search="Nike Pro sweatpants",
                                     prompt="a person wearing Nike Pro sweatpants")),
                      V, [], ["me"])
    assert p.action == "with_reference" and p.search == "Nike Pro sweatpants"
    assert p.source == "" and p.faces == "me"
    # The model cannot auto-pick a result, even if it invents a URL.
    assert p.reference == ""

    with pytest.raises(PlanInvalid, match="search"):
        validate_plan(json.dumps(raw(action="with_reference", faces="me")), V, [], ["me"])
    with pytest.raises(PlanInvalid, match="source photo or a face"):
        validate_plan(json.dumps(raw(action="with_reference", search="nike")), V, [], [])


def test_with_reference_may_edit_an_attached_photo():
    p = validate_plan(json.dumps(raw(action="with_reference", source="a1",
                                     search="red sneakers", prompt="red sneakers")),
                      V, ["a1"], [])
    assert p.source == "a1" and p.reference == ""


# --- policy ------------------------------------------------------------------
def test_a_reference_on_a_photo_is_an_img2img_and_uses_the_best_model():
    c = compile_plan(plan(action="with_reference", source="a1", search="garment",
                          prompt="a person wearing it"), V, REG, ctx((1024, 1024)))
    assert c.workflow == "with-reference-img2img-sdxl"
    assert c.checkpoint == XL
    assert c.raw["denoise"] == 0.55
    assert c.chip == "Finding a reference · RealVis XL"


def test_a_small_photo_keeps_the_reference_work_on_sd15():
    c = compile_plan(plan(action="with_reference", source="a1", search="garment",
                          prompt="a person wearing it"), V, REG, ctx((512, 512)))
    assert c.workflow == "with-reference-img2img-sdxl"
    assert c.checkpoint == RV6


def test_a_reference_with_no_photo_is_a_likeness_with_the_reference():
    c = compile_plan(plan(action="with_reference", faces="me", search="garment",
                          prompt="a person wearing it", reference="https://x/a.jpg"),
                     V, REG, ctx(None, True))
    assert c.workflow == "with-reference-likeness-sdxl"
    assert c.checkpoint == XL
    assert c.reference == "https://x/a.jpg"
    assert c.chip.startswith("Using a web reference · RealVis XL")


def test_a_reference_on_me_without_a_likeness_asks():
    with pytest.raises(NeedsInput, match="saved a likeness"):
        compile_plan(plan(action="with_reference", faces="me", search="garment"),
                     V, REG, ctx(None, False))


# --- graph shaping -----------------------------------------------------------
def test_the_garment_chain_is_optional():
    w = REG.require("with-reference-likeness-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "references": ["me.png"], "garment": "g.png",
                                 "aspect": "1:1"}))
    assert g["3"]["inputs"]["model"] == ["62", 0]
    assert g["60"]["inputs"]["model"] == ["32", 0]
    assert g["61"]["inputs"]["image"] == "g.png"
    assert g["62"]["inputs"]["image"] == ["61", 0]

    g2 = w.build_graph(w.resolve({"prompt": "x", "references": ["me.png"]}))
    assert "60" not in g2 and "61" not in g2 and "62" not in g2
    assert g2["3"]["inputs"]["model"] == ["32", 0]


def test_no_face_and_no_garment_removes_both_optional_chains():
    w = REG.require("with-reference-img2img-sdxl")
    g = w.build_graph(w.resolve({"prompt": "x", "image": "a.png"}))
    assert "30" not in g and "32" not in g and "60" not in g and "62" not in g
    assert g["3"]["inputs"]["model"] == ["4", 0]


# --- the turn engine: search, pick, fetch, queue -----------------------------
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


async def test_search_parks_with_candidates_then_fetches_the_pick(engine_harness, monkeypatch):
    from alchemyui import threads
    from alchemyui.planner.router import RouteResult
    from tests.conftest import _png_bytes

    h = engine_harness
    await h.likeness(h.alice)

    async def fake_search(query, cfg, **kw):
        assert query == "Nike Pro sweatpants"
        return [{"url": "https://cdn.example/nike.jpg",
                 "thumb_url": "https://cdn.example/nike-thumb.jpg",
                 "title": "Nike Pro", "source": "shop", "width": 900, "height": 1200}]

    async def fake_fetch(url, cfg, **kw):
        assert url == "https://cdn.example/nike.jpg"
        return _png_bytes((256, 256)), 256, 256

    monkeypatch.setattr(threads, "image_search", fake_search)
    monkeypatch.setattr(threads, "fetch_reference", fake_fetch)
    h.router.results = [RouteResult(P(action="with_reference", faces="me",
                                      search="Nike Pro sweatpants",
                                      prompt="a young man wearing Nike Pro sweatpants"),
                                    "primary")]
    t = await h.engine.create_turn(h.alice, "put me in Nike Pro sweatpants", [])
    parked = await settle(h, t["id"])
    assert parked["state"] == "needs_input"
    assert parked["question"]["candidates"][0]["thumb_url"].endswith("nike-thumb.jpg")
    assert parked["question"]["options"] == ["Nike Pro"]

    await h.engine.answer(h.alice, t["id"], None, 0)
    done = await settle(h, t["id"])
    assert done["state"] == "done"
    job = done["jobs"][0]
    assert job["workflow_id"] == "with-reference-likeness-sdxl"
    assert job["params"]["garment"].startswith("alchemyui/web-")
    assert job["params"]["references"]
