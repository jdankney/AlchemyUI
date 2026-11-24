"""The router's closed vocabulary: generated from the registry, with image
handles enumerated from the requesting user's own scope, and every plan
validated before code acts on it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alchemyui.planner.schema import Plan, PlanInvalid, Vocabulary, plan_schema, validate_plan
from alchemyui.registry import Registry, load_registry

REG = load_registry(Path(__file__).resolve().parent.parent / "workflows")
V = Vocabulary.from_registry(REG, max_count=4)


def _raw(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse at night",
                negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                direction="", question="", options=[])
    base.update(kw)
    return base


def test_vocabulary_comes_from_the_registry():
    assert V.actions == ("generate", "transform", "enhance", "likeness", "extend",
                         "edit_region", "with_reference", "select", "ask")
    assert V.workflows["generate"] == "txt2img-sdxl"      # never the -hq variant
    assert V.workflows["extend"] == "outpaint-sdxl"
    assert V.workflows["edit_region"] == "edit-region-sdxl"
    assert V.workflows["with_reference"] == "with-reference-likeness-sdxl"
    assert V.aspects == ("1:1", "3:4", "4:3", "9:16", "16:9")


def test_a_missing_workflow_removes_its_action():
    reg = Registry({k: w for k, w in REG._workflows.items() if w.kind != "likeness"},
                   REG._checkpoints)
    assert "likeness" not in Vocabulary.from_registry(reg, 4).actions


def test_schema_enumerates_only_handles_in_scope():
    s = plan_schema(V, sources=["a1", "t2.1"], faces=["me"])
    props = s["properties"]
    assert props["source"]["enum"] == ["", "a1", "t2.1"]
    assert props["faces"]["enum"] == ["", "me"]
    assert props["action"]["enum"] == list(V.actions)
    assert props["count"] == {"type": "integer", "minimum": 1, "maximum": 4}
    assert set(s["required"]) == set(props) and s["additionalProperties"] is False


def test_valid_plan_round_trips():
    p = validate_plan(json.dumps(_raw()), V, [], [])
    assert p.action == "generate" and Plan.from_dict(p.as_dict()) == p


@pytest.mark.parametrize("raw, why", [
    ("not json", "not valid JSON"),
    (_raw(action="edit_region", source="a1"), "needs region"),
    (_raw(action="edit_region", source="a1", region_mode="box"), "needs box"),
    (_raw(action="edit_region", source="a1", region_mode="box", box=[10, 20, 5, 30]),
     "right/bottom"),
    (_raw(action="transform", source=""), "needs a source image"),
    (_raw(action="transform", source="t9.9"), "t9.9"),
    (_raw(action="generate", source="a1"), "generate makes a new image"),
    (_raw(action="likeness", faces=""), "whose face"),
    (_raw(action="likeness", faces="t1.1"), "t1.1"),
    (_raw(action="generate", prompt="  "), "prompt"),
    (_raw(action="extend", source="a1", direction=""), "direction"),
    (_raw(action="ask", question=""), "question"),
    (_raw(aspect="7:5"), "aspect"),
    (_raw(action="select", source="a1"), "select picks an earlier image"),
])
def test_invalid_plans_say_why(raw, why):
    with pytest.raises(PlanInvalid, match=why):
        validate_plan(raw if isinstance(raw, str) else json.dumps(raw), V, ["a1"], ["me", "a1"])


def test_count_is_clamped_not_rejected():
    assert validate_plan(_raw(count=9), V, [], []).count == 4
    assert validate_plan(_raw(count=0), V, [], []).count == 1


def test_me_is_offered_unless_a_photo_of_them_is_the_only_option():
    """With no saved likeness, `me` can only become "go and save one" -- worth
    offering when nothing is attached, but not when the attachment is their face."""
    from alchemyui.planner.schema import face_handles
    assert face_handles(True, ["a1"]) == ["me", "a1"]
    assert face_handles(False, []) == ["me"]
    assert face_handles(False, ["a1", "a2"]) == ["a1", "a2"]


@pytest.mark.parametrize("raw, sources, faces", [
    (_raw(action="likeness", faces="a1"), ["a1", "a2"], ["a1", "a2"]),   # spec example 3
    (_raw(action="generate"), ["a1", "a2"], ["a1", "a2"]),               # "combine these"
    (_raw(action="likeness", faces="me"), ["a1"], ["me", "a1"]),         # "put these on me"
])
def test_a_new_picture_may_not_silently_ignore_an_attachment(raw, sources, faces):
    with pytest.raises(PlanInvalid, match="ignore"):
        validate_plan(json.dumps(raw), V, sources, faces)


def test_an_edit_may_use_just_one_of_several_attachments():
    """'make the second one snowy' with two attached is fine."""
    p = validate_plan(json.dumps(_raw(action="transform", source="a2")), V, ["a1", "a2"], [])
    assert p.source == "a2"
