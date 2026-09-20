"""App-owned image storage.

Generated images are copied OUT of ComfyUI's output/ into OUTPUT_DIR, one
directory per job. ComfyUI's folder layout is not a stable contract -- it
changes with filename_prefix, date tokens and custom nodes -- and an image the
gallery shows should not vanish because someone tidied up ComfyUI.

PNG dimensions are read straight from the IHDR chunk rather than pulling in
Pillow: it is 20 lines, and Phase 1 needs nothing else from an imaging library.
Phase 2's mask work and Phase 4's face normalisation will want the real thing.
"""

from __future__ import annotations

import io
import struct
import uuid
from pathlib import Path

from PIL import ImageOps

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_size(data: bytes) -> tuple[int | None, int | None]:
    """(width, height) from a PNG's IHDR, or (None, None) if it isn't one."""
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None, None
    # bytes 8..16 are the IHDR chunk length + type; 16..24 are width, height
    if data[12:16] != b"IHDR":
        return None, None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def save_output(output_dir: str | Path, job_id: str, filename: str, data: bytes) -> tuple[str, str]:
    """Write one generated image into app-owned storage.

    Returns (image_id, path). The image id is also the on-disk stem, so a row
    and its file can always be matched up by eye.
    """
    image_id = uuid.uuid4().hex
    suffix = Path(filename).suffix or ".png"
    job_dir = Path(output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / f"{image_id}{suffix}"
    path.write_bytes(data)
    return image_id, str(path)


def output_images(history: dict, output_node: str) -> list[dict]:
    """Pull the output-node image descriptors out of a /history entry.

    Falls back to every node that produced images: a template may route its
    save through a different node than the registry's declared output_node
    (a custom save node, an extra preview), and an image on disk is better
    than an empty gallery card.
    """
    outputs = (history or {}).get("outputs") or {}
    declared = (outputs.get(output_node) or {}).get("images") or []
    if declared:
        return [i for i in declared if i.get("type") == "output"] or declared

    found: list[dict] = []
    for node_output in outputs.values():
        for img in node_output.get("images") or []:
            if img.get("type") == "output":
                found.append(img)
    return found


# --------------------------------------------------------------------------- #
# uploads (Phase 2) -- img2img / inpaint / outpaint sources and their masks
# --------------------------------------------------------------------------- #
class UploadRejected(Exception):
    """The upload cannot be used. The message is shown to the person."""


def _snap8(n: int) -> int:
    """Round down to a multiple of 8.

    The VAE downscales by 8, so a dimension that isn't a multiple gets silently
    cropped inside VAEEncodeForInpaint -- which shifts the image relative to
    the mask the person painted. Doing it here, once, keeps source and mask
    provably aligned.
    """
    return max(8, (n // 8) * 8)


def normalise_upload(data: bytes, max_edge: int, max_mb: float) -> tuple[bytes, int, int]:
    """Validate an uploaded image and return (png_bytes, width, height).

    Everything is converted to RGB PNG at a size the GPU can actually take:
    a 6GB card OOMs on a 4000px phone photo long before it runs out of
    patience, and SDXL is trained at ~1024 anyway.
    """
    from PIL import Image, UnidentifiedImageError

    if len(data) > max_mb * 1024 * 1024:
        raise UploadRejected(f"That image is larger than {max_mb:.0f} MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise UploadRejected("That file isn't an image we can read (try PNG, JPEG or WebP).")

    # EXIF orientation is applied now rather than carried: the mask is painted
    # against what the browser displayed, so the pixels must match that.
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        w, h = round(w * scale), round(h * scale)
    w, h = _snap8(w), _snap8(h)
    if (w, h) != img.size:
        img = img.resize((w, h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), w, h


def normalise_mask(data: bytes, width: int, height: int) -> bytes:
    """Validate a painted mask and return it as a black-and-white PNG.

    The convention, end to end: **white means repaint**. That is what the
    person paints, it is what LoadImageMask reads out of the red channel, and
    it is what VAEEncodeForInpaint treats as the region to replace. Keeping one
    polarity the whole way is the difference between inpainting the thing you
    circled and inpainting everything except it.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        mask = Image.open(io.BytesIO(data))
        mask.load()
    except (UnidentifiedImageError, OSError):
        raise UploadRejected("The mask isn't a readable image.")

    # A mask drawn on a canvas is RGBA with transparent un-painted areas;
    # flattening onto black makes "painted" unambiguous whichever the browser
    # gave us.
    if mask.mode in ("RGBA", "LA", "P"):
        mask = mask.convert("RGBA")
        flat = Image.new("RGBA", mask.size, (0, 0, 0, 255))
        flat.alpha_composite(mask)
        mask = flat
    mask = mask.convert("L")

    if mask.size != (width, height):
        # Resized rather than refused: the canvas may be displayed scaled, and
        # a nearest-neighbour resize keeps the edges hard.
        mask = mask.resize((width, height), Image.NEAREST)

    if not mask.getbbox():
        raise UploadRejected("Nothing was painted -- brush over the area you want changed.")

    buf = io.BytesIO()
    mask.point(lambda v: 255 if v > 127 else 0).convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def save_upload(upload_dir: str | Path, data: bytes, stem: str, suffix: str = ".png") -> str:
    d = Path(upload_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stem}{suffix}"
    path.write_bytes(data)
    return str(path)
