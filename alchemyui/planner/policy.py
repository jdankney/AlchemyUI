"""Meaning in, numbers out (spec section 7.3).

The model picks words -- subtle, fast, wider -- and this file turns them into
denoise values, checkpoints and pixel counts. The numbers live here, where
they were measured, rather than in a small model's guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..profiles import compose
from ..registry import CheckpointInfo, Registry, WorkflowInfo
from .schema import Plan, Vocabulary

DENOISE = {"subtle": 0.35, "medium": 0.55, "strong": 0.75}
# SDXL is trained at 1024 and turns a small source to mush; below this edge an
# image stays on SD1.5 whatever quality was asked for.
SDXL_MIN_SOURCE_EDGE = 900
VERB = {"generate": "Generating", "transform": "Transforming", "enhance": "Enhancing",
        "likeness": "Generating you", "extend": "Extending"}
# Only these make several images; one enhance or extend of the same source
# four times over is the same picture four times.
COUNTABLE = ("generate", "transform", "likeness")


@dataclass(frozen=True)
class PolicyContext:
    resident: str | None                   # checkpoint ComfyUI is believed to hold
    source_size: tuple[int, int] | None    # (w, h) of the resolved source image
    has_likeness: bool
    pinned: str | None = None              # the model the person picked, or None for Auto


@dataclass(frozen=True)
class Compiled:
    action: str
    workflow: str | None                   # None: nothing to queue (select)
    checkpoint: str | None
    raw: dict                              # what submit_jobs hands workflow.resolve
    count: int
    chip: str                              # "Generating · Realistic Vision 6.0 · 3:4"
    pinned: str | None = None              # what the person picked, whether or not it was used
    note: str = ""                         # why the pick was not used, when it was not

    def as_dict(self) -> dict:
        return asdict(self)


class NeedsInput(Exception):
    """The turn stops and asks. `options` are [{"label", "plan"}]; a plan on an
    option runs as-is when picked, a bare label is replayed to the router."""

    def __init__(self, question: str, options: list[dict] | None = None):
        super().__init__(question)
        self.question = question
        self.options = options or []


def pick_checkpoint(reg: Registry, workflow: WorkflowInfo, trait: str, resident: str | None,
                    family: str | None = None) -> CheckpointInfo:
    """The first checkpoint with the trait that this workflow can run, in
    catalogue order -- unless one of them is already loaded, which saves a
    30s swap."""
    usable = [c for c in reg.all_checkpoints() if c.family in workflow.families]
    if family:
        usable = [c for c in usable if c.family == family] or usable
    cands = [c for c in usable if trait in c.traits]
    if not cands:
        return reg.checkpoint(workflow.checkpoint) or usable[0]
    for c in cands:
        if c.name == resident:
            return c
    return cands[0]


def _pads(direction: str, size: tuple[int, int] | None) -> dict:
    """How far to extend, as a share of the edge being extended. Snapped to 8
    (the VAE's stride) and held to what the outpaint template allows."""
    w, h = size or (512, 512)

    def px(edge: int, share: float) -> int:
        return max(64, min(512, int(edge * share) // 8 * 8))

    pads = dict(pad_left=0, pad_right=0, pad_top=0, pad_bottom=0)
    if direction == "wider":
        pads.update(pad_left=px(w, .25), pad_right=px(w, .25))
    elif direction == "taller":
        pads.update(pad_top=px(h, .25), pad_bottom=px(h, .25))
    elif direction == "all":
        pads = dict(pad_left=px(w, .2), pad_right=px(w, .2),
                    pad_top=px(h, .2), pad_bottom=px(h, .2))
    else:
        side = {"left": "pad_left", "right": "pad_right", "up": "pad_top", "down": "pad_bottom"}
        pads[side[direction]] = px(w if direction in ("left", "right") else h, .5)
    return pads


def compile_plan(plan: Plan, vocab: Vocabulary, reg: Registry, ctx: PolicyContext) -> Compiled:
    if plan.action == "ask":
        raise NeedsInput(plan.question, [{"label": o, "plan": None} for o in plan.options])
    if plan.action == "select":
        return Compiled("select", None, None, {}, 0, "Working from the image you picked")
    if plan.action == "likeness" and plan.faces == "me" and not ctx.has_likeness:
        raise NeedsInput("You haven't saved a likeness yet. Attach a clear photo of your "
                         "face, or add some in Settings, then ask again.")

    workflow = reg.require(vocab.workflows[plan.action])
    family = None
    if plan.action == "extend":
        trait = "inpaint"
    elif plan.quality == "best":
        trait = "quality"
    else:
        trait = "fast"
    if (plan.action in ("transform", "extend") and ctx.source_size
            and max(ctx.source_size) < SDXL_MIN_SOURCE_EDGE):
        family = "sd15-inpaint" if plan.action == "extend" else "sd15"
        if trait == "quality":
            trait = "fast"
    ck = pick_checkpoint(reg, workflow, trait, ctx.resident, family)
    ck, note = _honour_pin(reg, workflow, plan, ctx, ck)

    house = str(workflow.defaults.get("negative") or "")
    prompt, negative = compose(plan.prompt, plan.negative, ck, house)
    raw: dict = {"prompt": prompt, "negative": negative}
    if "width" in workflow.params:
        raw["aspect"] = plan.aspect
    if plan.action == "transform":
        raw["denoise"] = DENOISE[plan.strength]
    if plan.action == "extend":
        raw.update(_pads(plan.direction, ctx.source_size))

    count = plan.count if plan.action in COUNTABLE else 1
    parts = [VERB[plan.action] + (f" {count}" if count > 1 else ""), ck.label]
    if "aspect" in raw:
        parts.append(plan.aspect)
    return Compiled(plan.action, workflow.key, ck.name, raw, count, " · ".join(parts),
                    ctx.pinned, note)


def _honour_pin(reg: Registry, workflow: WorkflowInfo, plan: Plan, ctx: PolicyContext,
                picked: CheckpointInfo) -> tuple[CheckpointInfo, str]:
    """A person's pick replaces the policy's -- except where honouring it
    would break the picture, and then the policy's pick stands and the note
    says why, in words for the person who made the pick."""
    pin = reg.checkpoint(ctx.pinned) if ctx.pinned else None
    if pin is None:
        return picked, ""
    if plan.action == "extend":
        return picked, f"Not {pin.label}: extending always uses the inpainting model."
    if pin.family not in workflow.families:
        return picked, f"Not {pin.label}: this needs an SDXL model."
    if (plan.action == "transform" and pin.family == "sdxl" and ctx.source_size
            and max(ctx.source_size) < SDXL_MIN_SOURCE_EDGE):
        return picked, f"Not {pin.label}: the source is too small for SDXL."
    return pin, ""
