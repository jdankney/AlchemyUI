"""What the router is told. Plain words, one screen long: a 4B model follows
a short, concrete brief far better than a long one."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from .schema import DIRECTIONS, Vocabulary

MEANING = {
    "generate": "make a brand-new image from words alone (no source image)",
    "transform": "restyle or change the whole of an existing image: 'make it darker', "
                 "'as an oil painting', 'add snow'",
    "enhance": "sharpen, upscale or fix the faces in an existing image, keeping it the same",
    "likeness": "a new image of a person whose face we have: 'me as an astronaut'",
    "extend": "make an existing image bigger by painting beyond its edges",
    "edit_region": "change one part of an existing image: replace something that is there "
                   "('the sky', 'the dog') or put something new into it ('me, on the bed')",
    "with_reference": "find a reference picture on the web (a garment, an object, a place) "
                      "and use it to make or edit an image",
    "select": "the user only picks which earlier image to work from next ('use the second one')",
    "ask": "you cannot tell what they want, or they want something we cannot do yet",
}


@dataclass(frozen=True)
class Attachment:
    handle: str
    data_url: str


def thumbnail(data: bytes, edge: int) -> str:
    """A JPEG data URL no larger than `edge`. Only this turn's attachments are
    ever sent; earlier outputs are described by their prompts instead."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((edge, edge), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def system_prompt(vocab: Vocabulary) -> str:
    actions = "\n".join(f"- {a}: {MEANING[a]}" for a in vocab.actions)
    return f"""You plan image requests for an image generator. Reply with exactly one JSON plan.

Actions:
{actions}

Images are named by handles: a1, a2... are this message's attachments in order;
t4.2 is the second image made by turn 4 of this conversation; "me" is the user's
saved face. Only use handles you are given.

Rules:
- source: the image to work from (transform, enhance, extend, select). "" for generate.
- faces: for likeness, "me" or the attachment that shows their face. "" otherwise.
- prompt: rewrite the request as a Stable Diffusion prompt in English, comma-separated,
  under 60 words: subject first, then setting, lighting, style and photo details. For
  transform, describe the whole finished image, not just the change.
- negative: a few things to avoid, or "".
- strength: how much a transform changes the image: subtle, medium or strong.
- quality: "fast" unless they ask for best, highest, detailed or professional quality.
- count: how many images they asked for (default 1).
- aspect: {', '.join(vocab.aspects)}. Portraits 3:4, landscapes 16:9, otherwise 1:1.
- direction: for extend only: {', '.join(DIRECTIONS[1:])}. Otherwise "".
- search and reference: for with_reference only. search is a short web image
  query ("Nike Pro sweatpants product photo"); reference is always "" -- the app
  fills it after the person picks a result. Otherwise "" and "".
- region and region_mode: for edit_region only. region is the part to change, in a few
  words ("the sky", "the dog"). region_mode is "segment" when replacing something that
  is already there, or "box" when placing something new. Otherwise "" and "".
- box: for a box edit, [x1, y1, x2, y2] on a 0..1000 scale (top-left is 0,0). For
  "add me to the bed", put a portrait-shaped box where the person should appear. []
  otherwise. Never put a box outside the image.
- question and options: for ask only, with up to 4 short options. Otherwise "" and [].
- Follow-ups like "make it darker" work on the current image.
- If the conversation says "none picked yet" and the message does not say which image,
  ask which one, with options like "Image 1" and "Image 2".
- For edit_region, source is the photo to change; the prompt describes what the changed
  region should contain, not a whole new picture. If a different attachment shows the
  person's face, put that attachment in faces.
- For with_reference, search names the garment or object to find. If there is a photo to
  edit, put it in source; otherwise use faces ("me" or an attachment) and the app will
  make a new picture of that person wearing or holding the reference.
- If they want two photos combined into one, ask which photo should be changed and what
  should go into it; that is an edit_region, not a new picture.
- "Me" with an attached photo of a face and no saved likeness: faces is that attachment."""


def build_messages(vocab: Vocabulary, text: str, attachments: list[Attachment],
                   context: list[str], has_likeness: bool, answers: list[dict],
                   images: bool = True) -> list[dict]:
    lines = []
    if context:
        lines.append("Conversation so far:")
        lines.extend(context)
    lines.append(f"The user has a saved likeness: {'yes' if has_likeness else 'no'}.")
    if attachments:
        lines.append("This message has " + ", ".join(a.handle for a in attachments)
                     + " attached" + ("" if images else " (you cannot see them)") + ".")
    lines.append(f'The user says: "{text.strip()}"' if text.strip()
                 else "The user sent only the attachments, with no words.")
    for qa in answers:
        lines.append(f'You asked: "{qa["question"]}" They answered: "{qa["answer"]}"')
    parts: list[dict] = [{"type": "text", "text": "\n".join(lines)}]
    if images:
        for a in attachments:
            parts.append({"type": "text", "text": f"{a.handle}:"})
            parts.append({"type": "image_url", "image_url": {"url": a.data_url}})
    return [{"role": "system", "content": system_prompt(vocab)},
            {"role": "user", "content": parts}]


def repair_message(error: str) -> dict:
    return {"role": "user",
            "content": f"That plan was rejected: {error}. Reply with a corrected plan."}
