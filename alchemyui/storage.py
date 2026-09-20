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

import struct
import uuid
from pathlib import Path

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
