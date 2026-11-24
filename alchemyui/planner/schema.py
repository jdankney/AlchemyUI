"""The router's closed vocabulary (spec section 7).

Generated, never hand-listed: an action exists only if a validated workflow
backs it, so Phase 8's `edit_region` cannot be offered before it works. On
top of that, `source` and `faces` are enums of the handles that exist in the
requesting user's scope *for this turn*, so the grammar cannot even emit a
handle that would fail to resolve. Resolution still checks (hard rule 3).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ..registry import Registry

ACTIONS_KIND = {"generate": "txt2img", "transform": "img2img", "enhance": "enhance",
                "likeness": "likeness", "extend": "outpaint",
                "edit_region": "edit_region", "with_reference": "with_reference"}
NO_GPU = ("select", "ask")
NEEDS_SOURCE = ("transform", "enhance", "extend", "edit_region", "select")
NEEDS_PROMPT = ("generate", "transform", "likeness", "extend", "edit_region",
                "with_reference")
STRENGTHS = ("subtle", "medium", "strong")
QUALITIES = ("fast", "best")
DIRECTIONS = ("", "wider", "taller", "left", "right", "up", "down", "all")
REGION_MODES = ("", "segment", "box")
MAX_PROMPT = 600
MAX_NEGATIVE = 240
MAX_QUESTION = 240
MAX_REGION = 160
MAX_SEARCH = 240


def face_handles(has_likeness: bool, attachments: list[str]) -> list[str]:
    """Who a likeness may be of. `me` means the requester's saved likeness and
    nobody else's. With none saved it can only become "save one first" --
    worth offering when nothing is attached, but not when an attachment is
    very likely the face they mean (router-eval: "this is me, make me a
    knight" went to `me` and asked for a likeness they had just attached)."""
    return (["me"] if has_likeness or not attachments else []) + list(attachments)


class PlanInvalid(Exception):
    """A plan that parsed but does not hold together. The message is fed back
    to the model verbatim on the repair retry, so it says what to change."""


@dataclass(frozen=True)
class Vocabulary:
    actions: tuple[str, ...]
    workflows: dict[str, str]          # action -> workflow key
    aspects: tuple[str, ...]
    max_count: int

    @classmethod
    def from_registry(cls, reg: Registry, max_count: int) -> Vocabulary:
        workflows: dict[str, str] = {}
        for action, kind in ACTIONS_KIND.items():
            # The -hq variants are reached by quality, never as an action.
            # txt2img-sdxl declares no kind and so defaults to txt2img.
            found = [w for w in reg.of_kind(kind) if not w.key.endswith("-hq")]
            if found:
                workflows[action] = found[0].key
        actions = tuple(a for a in ACTIONS_KIND if a in workflows) + NO_GPU
        gen = reg.get(workflows.get("generate", ""))
        ck = reg.checkpoint(gen.checkpoint) if gen else None
        aspects = tuple(ck.aspects if ck and ck.aspects else (gen.aspects if gen else {}))
        return cls(actions, workflows, aspects or ("1:1",), max(1, max_count))


@dataclass(frozen=True)
class Plan:
    action: str
    source: str
    faces: str
    prompt: str
    negative: str
    aspect: str
    count: int
    strength: str
    quality: str
    direction: str
    question: str
    options: tuple[str, ...]
    # Phase 8: a region edit names either what to segment ("the sky") or the
    # normalized box where something new goes ([x1, y1, x2, y2], 0..1000).
    # They are always present in the schema, and empty for every other action.
    region: str = ""
    region_mode: str = ""
    box: tuple[int, int, int, int] | tuple = ()
    # Phase 9: the phrase to search for, and the URL the person picked. The
    # URL is server-written after a candidate is chosen, never model-written.
    search: str = ""
    reference: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["options"] = list(self.options)
        d["box"] = list(self.box)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        return cls(**{**d, "options": tuple(d.get("options") or ()),
                      "box": tuple(d.get("box") or ())})


def plan_schema(vocab: Vocabulary, sources: list[str], faces: list[str]) -> dict:
    props = {
        "action": {"enum": list(vocab.actions)},
        "source": {"enum": ["", *sources]},
        "faces": {"enum": ["", *faces]},
        "prompt": {"type": "string", "maxLength": MAX_PROMPT},
        "negative": {"type": "string", "maxLength": MAX_NEGATIVE},
        "aspect": {"enum": list(vocab.aspects)},
        "count": {"type": "integer", "minimum": 1, "maximum": vocab.max_count},
        "strength": {"enum": list(STRENGTHS)},
        "quality": {"enum": list(QUALITIES)},
        "direction": {"enum": list(DIRECTIONS)},
        "question": {"type": "string", "maxLength": MAX_QUESTION},
        "options": {"type": "array", "maxItems": 4,
                    "items": {"type": "string", "maxLength": 60}},
        "region": {"type": "string", "maxLength": MAX_REGION},
        "region_mode": {"enum": list(REGION_MODES)},
        "box": {"type": "array", "maxItems": 4,
                "items": {"type": "integer", "minimum": 0, "maximum": 1000}},
        "search": {"type": "string", "maxLength": MAX_SEARCH},
    }
    # Every field required, none optional: a flat, fully-required object is
    # the grammar llama.cpp enforces most reliably, and a small model fills in
    # a neutral "" far better than it decides whether a key should exist.
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


def validate_plan(raw: str | dict, vocab: Vocabulary, sources: list[str],
                  faces: list[str]) -> Plan:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanInvalid(f"the reply was not valid JSON ({e.msg})")
    if not isinstance(raw, dict):
        raise PlanInvalid("the reply was not a JSON object")

    def s(key: str, limit: int = 1000) -> str:
        return str(raw.get(key) or "").strip()[:limit]

    action = s("action")
    if action not in vocab.actions:
        raise PlanInvalid(f"action {action!r} is not one of {', '.join(vocab.actions)}")
    source, face = s("source"), s("faces")
    if source and source not in sources:
        raise PlanInvalid(f"source {source!r} is not an image in this conversation")
    if face and face not in faces:
        raise PlanInvalid(f"faces {face!r} is not a face this user has provided")
    if action in NEEDS_SOURCE and not source:
        raise PlanInvalid(f"{action} needs a source image; set source to one of "
                          f"{', '.join(sources) or '(none -- use ask instead)'}")
    if action == "select" and source.startswith("a"):
        raise PlanInvalid("select picks an earlier image to keep working from; for an attached "
                          "image, choose what to do with it, or ask")
    if action == "generate" and source:
        raise PlanInvalid("generate makes a new image from words only; to change an "
                          "existing image use transform, enhance or extend with that source")
    if action == "likeness" and not face:
        raise PlanInvalid("likeness needs to know whose face: set faces to "
                          f"{' or '.join(faces) or 'me'}")
    prompt = s("prompt", MAX_PROMPT)
    if action in NEEDS_PROMPT and not prompt:
        raise PlanInvalid(f"{action} needs a prompt describing the image to make")
    direction = s("direction")
    if direction not in DIRECTIONS:
        raise PlanInvalid(f"direction {direction!r} is not one of {', '.join(DIRECTIONS[1:])}")
    if action == "extend" and not direction:
        raise PlanInvalid("extend needs a direction: " + ", ".join(DIRECTIONS[1:]))
    question = s("question", MAX_QUESTION)
    if action == "ask" and not question:
        raise PlanInvalid("ask needs a question for the user")
    aspect = s("aspect") or vocab.aspects[0]
    if aspect not in vocab.aspects:
        raise PlanInvalid(f"aspect {aspect!r} is not one of {', '.join(vocab.aspects)}")
    # A new picture that leaves an attached photo out is dropping what the
    # user gave us; say so, so the repair retry can ask instead. Edits are
    # exempt: "make the second one snowy" rightly uses only a2.
    if action in ("generate", "likeness"):
        unused = [h for h in sources if h.startswith("a") and h != face]
        if unused:
            # Ask comes first: the model follows the first remedy offered, and
            # an attachment we cannot use as a reference is a question, not an
            # edit of that photo (router-eval: "put these on me" became a
            # transform of the sweatpants when the edit was offered first).
            raise PlanInvalid(f"{action} would ignore the attached {', '.join(unused)}. We cannot "
                              "use a photo as a reference yet, so ask what they want; only if they "
                              "want that photo itself changed, make it the source of an edit")
    strength = s("strength") if s("strength") in STRENGTHS else "medium"
    quality = s("quality") if s("quality") in QUALITIES else "fast"
    try:
        count = int(raw.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    options = tuple(str(o).strip()[:60] for o in (raw.get("options") or [])
                    if str(o).strip())[:4]

    search = s("search", MAX_SEARCH)
    # The model never picks a web result. The app fills this only in a forced
    # option after the person clicks a candidate; constrained decoding cannot
    # even emit it (there is no such property in plan_schema).
    reference = ""
    if action == "with_reference":
        if not search:
            raise PlanInvalid("with_reference needs search (what to look for on the web)")
        if not source and not face:
            raise PlanInvalid("with_reference needs a source photo or a face to put the "
                              "reference on")
    else:
        search, reference = "", ""

    region = s("region", MAX_REGION)
    region_mode = s("region_mode")
    if region_mode not in REGION_MODES:
        raise PlanInvalid(f"region_mode {region_mode!r} is not one of "
                          f"{', '.join(m for m in REGION_MODES if m)}")
    box: tuple[int, ...] = ()
    if action == "edit_region":
        raw_box = raw.get("box") or []
        if not isinstance(raw_box, (list, tuple)):
            raise PlanInvalid("box must be a list of four numbers")
        if raw_box:
            try:
                box = tuple(int(v) for v in raw_box)
            except (TypeError, ValueError):
                raise PlanInvalid("box must contain four whole numbers")
            if len(box) != 4 or any(v < 0 or v > 1000 for v in box):
                raise PlanInvalid("box must contain exactly four whole numbers "
                                  "[x1, y1, x2, y2] between 0 and 1000")
            if box[2] <= box[0] or box[3] <= box[1]:
                raise PlanInvalid("box needs a right/bottom edge past its left/top edge")
        if not region_mode:
            region_mode = "box" if box else "segment" if region else ""
        if region_mode == "box" and not box:
            raise PlanInvalid("a box edit needs box [x1, y1, x2, y2]")
        if region_mode == "segment" and not region:
            raise PlanInvalid("a segmented edit needs region (what to change, e.g. 'the sky')")
        if not region_mode:
            raise PlanInvalid("edit_region needs region (what to change) or box "
                              "(where to put something new)")
        if region_mode == "segment":
            box = ()
    else:
        # The model fills every field; only an edit_region may use these.
        region, region_mode, box = "", "", ()
    return Plan(action, source, face, prompt, s("negative", MAX_NEGATIVE), aspect,
                max(1, min(vocab.max_count, count)), strength, quality, direction,
                question, options, region, region_mode, box, search, reference)
