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
- question and options: for ask only, with up to 4 short options. Otherwise "" and [].
- Follow-ups like "make it darker" work on the current image. If the last turn made
  several images and they have not picked one, ask which.
- If they want one part of a photo changed, or someone or something added into a
  particular photo, you cannot do that yet: ask, and offer a whole-image alternative."""


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
