"""Phase 2 uploads: normalisation, and the mask polarity the whole editing
path depends on.

Mask convention, asserted here because getting it backwards silently inpaints
everything *except* what the person circled: **white means replace**.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from alchemyui.storage import UploadRejected, _snap8, normalise_mask, normalise_upload


def _png(size, colour=(120, 90, 200), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(size) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(buf, format="JPEG")
    return buf.getvalue()


# --- source images -----------------------------------------------------------
def test_dimensions_are_snapped_to_multiples_of_eight():
    """The VAE downscales by 8 and silently crops anything else -- which would
    shift the image relative to the mask painted over it."""
    _, w, h = normalise_upload(_png((1023, 767)), 1536, 24)
    assert w % 8 == 0 and h % 8 == 0
    assert (w, h) == (1016, 760)
    assert _snap8(3) == 8            # never zero


def test_oversized_images_are_scaled_to_fit_the_gpu():
    _, w, h = normalise_upload(_png((4000, 3000)), 1536, 24)
    assert max(w, h) == 1536
    assert abs((w / h) - (4000 / 3000)) < 0.02      # aspect preserved
    assert w % 8 == 0 and h % 8 == 0


def test_small_images_are_left_alone():
    _, w, h = normalise_upload(_png((512, 768)), 1536, 24)
    assert (w, h) == (512, 768)


def test_jpeg_and_rgba_become_rgb_png():
    png, _, _ = normalise_upload(_jpeg((800, 600)), 1536, 24)
    assert png.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(png)).mode == "RGB"

    png2, _, _ = normalise_upload(_png((400, 400), (10, 20, 30, 255), "RGBA"), 1536, 24)
    assert Image.open(io.BytesIO(png2)).mode == "RGB"


def test_oversized_and_unreadable_uploads_are_refused_in_words():
    with pytest.raises(UploadRejected, match="larger than"):
        normalise_upload(_png((100, 100)), 1536, 0.00001)
    with pytest.raises(UploadRejected, match="isn't an image"):
        normalise_upload(b"this is not an image", 1536, 24)


def test_exif_rotation_is_applied_not_carried():
    """The mask is painted against what the browser displayed. If the pixels
    still need rotating, the mask lines up with nothing."""
    img = Image.new("RGB", (400, 200), (200, 30, 30))
    exif = Image.Exif()
    exif[274] = 6                      # orientation: rotate 90
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    _, w, h = normalise_upload(buf.getvalue(), 1536, 24)
    assert (w, h) == (200, 400)        # landscape became portrait


# --- masks -------------------------------------------------------------------
def _painted(size, box=(50, 50, 150, 150), mode="RGBA") -> bytes:
    m = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else 0)
    fill = (255, 255, 255, 255) if mode == "RGBA" else 255
    ImageDraw.Draw(m).ellipse(box, fill=fill)
    buf = io.BytesIO()
    m.save(buf, format="PNG")
    return buf.getvalue()


def test_white_means_replace_and_the_red_channel_carries_it():
    """LoadImageMask reads the red channel; VAEEncodeForInpaint replaces where
    the mask is 1. So painted pixels must come out as red=255."""
    out = normalise_mask(_painted((512, 512)), 512, 512)
    img = Image.open(io.BytesIO(out))
    r = img.convert("RGB").split()[0]
    assert r.getpixel((100, 100)) == 255      # inside the painted ellipse
    assert r.getpixel((5, 5)) == 0            # outside it


def test_mask_is_binary_not_antialiased():
    """Half-lit edge pixels become a partial noise mask and a soft, smeared
    seam. grow_mask_by is what feathers the join, deliberately."""
    out = normalise_mask(_painted((256, 256)), 256, 256)
    values = set(Image.open(io.BytesIO(out)).convert("L").tobytes())
    assert values <= {0, 255}


def test_transparent_canvas_export_is_flattened_onto_black():
    """A canvas mask arrives RGBA with un-painted areas transparent. Composited
    onto white by accident, the whole image would read as 'replace'."""
    out = normalise_mask(_painted((300, 300), mode="RGBA"), 300, 300)
    assert Image.open(io.BytesIO(out)).convert("L").getpixel((2, 2)) == 0


def test_mask_is_resized_to_the_source_rather_than_refused():
    out = normalise_mask(_painted((256, 256)), 512, 512)
    assert Image.open(io.BytesIO(out)).size == (512, 512)


def test_an_unpainted_mask_is_refused_with_a_useful_message():
    blank = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    with pytest.raises(UploadRejected, match="Nothing was painted"):
        normalise_mask(buf.getvalue(), 256, 256)
