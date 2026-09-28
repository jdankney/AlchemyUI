"""Profiles: a model's tuned defaults, overlaid on the registry's. The registry
file stays the baseline; a profile changes what every job on that model runs
with until it is reset."""

from __future__ import annotations

import json

import pytest

from alchemyui.db import Database
from alchemyui.profiles import (
    ProfileInvalid,
    compose,
    load_profiles,
    reset_profile,
    save_profile,
    validate,
)
from alchemyui.registry import load_registry
from tests.conftest import REPO

RV6 = "SD1.5/realisticVisionV60B1_v60B1VAE_pruned_fp16.safetensors"
XL = "SDXL/RealVisXL_V5.0_fp16.safetensors"
HYPER = "SD1.5/realisticVisionV60B1_v51HyperVAE_pruned_fp16.safetensors"
EPIC = "SD1.5/epicrealism_naturalSinRC1VAE.safetensors"


@pytest.fixture
def reg():
    return load_registry(REPO / "workflows")


def test_switchable_models_are_the_photoreal_ones_best_first(reg):
    names = [c.name for c in reg.switchable()]
    assert names == [XL, RV6, EPIC, HYPER]
    assert all(c.tagline for c in reg.switchable())


def test_an_override_changes_what_a_job_resolves_to_and_a_reset_restores_it(reg):
    wf = reg.require("txt2img-sdxl")
    before = wf.resolve({"prompt": "x"}, reg.checkpoint(RV6))
    reg.set_override(RV6, {"steps": 30, "cfg": 4.0, "sampler": "euler", "scheduler": "normal"})
    after = wf.resolve({"prompt": "x"}, reg.checkpoint(RV6))
    assert (after["steps"], after["cfg"], after["sampler"], after["scheduler"]) == (
        30, 4.0, "euler", "normal")
    assert reg.base(RV6).defaults == {"steps": 25, "cfg": 5.0}
    reg.set_override(RV6, None)
    assert wf.resolve({"prompt": "x"}, reg.checkpoint(RV6))["cfg"] == before["cfg"]


def test_validate_clamps_numbers_and_refuses_what_the_workflow_cannot_run(reg):
    assert validate({"steps": 500, "cfg": 0.1}, reg) == {"steps": 80, "cfg": 1.0}
    assert validate({"look": "  RAW   photo  "}, reg) == {"look": "RAW photo"}
    for bad in ({"sampler": "nope"}, {"scheduler": "nope"}, {"steps": "many"},
                {"colour": "red"}):
        with pytest.raises(ProfileInvalid):
            validate(bad, reg)


def test_compose_appends_the_look_and_prefers_the_models_own_negative(reg):
    house = "blurry, watermark"
    ck = reg.checkpoint(RV6)
    assert compose("a cat", "dogs", ck, house) == ("a cat", "dogs, blurry, watermark")
    reg.set_override(RV6, {"look": "RAW photo, film grain", "negative": "cgi, 3d render"})
    ck = reg.checkpoint(RV6)
    assert compose("a cat", "dogs", ck, house) == ("a cat, RAW photo, film grain",
                                                    "dogs, cgi, 3d render")
    # an explicitly empty negative means none at all, not the house one
    reg.set_override(RV6, {"negative": ""})
    assert compose("a cat", "", reg.checkpoint(RV6), house) == ("a cat", "")


async def test_profiles_persist_and_load_back_onto_a_fresh_registry(tmp_db_path, reg):
    db = Database(tmp_db_path)
    await db.execute("INSERT INTO users (oidc_sub, email, created_at) VALUES ('a', 'a@x', 1)")
    saved = await save_profile(db, reg, RV6, {"cfg": 3.5, "look": "35mm film"}, 1)
    assert saved == {"cfg": 3.5, "look": "35mm film"}
    row = await db.fetch_one("SELECT * FROM model_profiles WHERE checkpoint = ?", (RV6,))
    assert json.loads(row["settings_json"]) == saved

    fresh = load_registry(REPO / "workflows")
    await load_profiles(db, fresh)
    assert fresh.checkpoint(RV6).defaults["cfg"] == 3.5
    assert fresh.checkpoint(RV6).defaults["look"] == "35mm film"

    await reset_profile(db, fresh, RV6)
    assert fresh.checkpoint(RV6).defaults == {"steps": 25, "cfg": 5.0}
    assert await db.fetch_one("SELECT 1 FROM model_profiles") is None
    db.close()


async def test_a_profile_is_only_for_a_switchable_model(tmp_db_path, reg):
    db = Database(tmp_db_path)
    with pytest.raises(ProfileInvalid):
        await save_profile(db, reg, "SD1.5/v1-5-pruned-emaonly.safetensors", {"cfg": 5}, None)
    db.close()
