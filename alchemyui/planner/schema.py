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
                "likeness": "likeness", "extend": "outpaint"}
NO_GPU = ("select", "ask")
NEEDS_SOURCE = ("transform", "enhance", "extend", "select")
NEEDS_PROMPT = ("generate", "transform", "likeness", "extend")
STRENGTHS = ("subtle", "medium", "strong")
QUALITIES = ("fast", "best")
DIRECTIONS = ("", "wider", "taller", "left", "right", "up", "down", "all")
MAX_PROMPT = 600
MAX_NEGATIVE = 240
MAX_QUESTION = 240


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

    def as_dict(self) -> dict:
        d = asdict(self)
        d["options"] = list(self.options)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        return cls(**{**d, "options": tuple(d.get("options") or ())})


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
    strength = s("strength") if s("strength") in STRENGTHS else "medium"
    quality = s("quality") if s("quality") in QUALITIES else "fast"
    try:
        count = int(raw.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    options = tuple(str(o).strip()[:60] for o in (raw.get("options") or [])
                    if str(o).strip())[:4]
    return Plan(action, source, face, prompt, s("negative", MAX_NEGATIVE), aspect,
                max(1, min(vocab.max_count, count)), strength, quality, direction,
                question, options)
