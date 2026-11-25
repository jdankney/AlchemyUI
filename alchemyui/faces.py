"""Reference-face handling for likeness generation (Phase 4).

Two failure modes the spec says it has already hit in practice, and what this
module does about them:

1. InsightFace raises "InsightFace: No face detected." if *any* image in a
   batch fails detection, and the whole job dies -- after the queue wait, after
   the model load. So every reference is detected on upload and rejected there,
   by name, with a message that says what to do.

2. `ImageBatch` rescales and centre-crops later images to match the *first*
   image's dimensions, which can slice a face out of frame. So every reference
   is normalised to the same square size with the face centred before it ever
   reaches ComfyUI -- ImageBatch then has nothing left to do.

Detection runs on the CPU, deliberately: onnxruntime-gpu is not installed and
the GPU belongs to ComfyUI. buffalo_l is small and a single detection takes
well under a second, so this stays out of the way.
"""

from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import Settings

log = logging.getLogger("alchemyui.faces")


class FaceRejected(Exception):
    """A reference image cannot be used. The message is shown to the person."""


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[float, float, float, float]
    score: float

    @property
    def size(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(x2 - x1, y2 - y1)

    @property
    def centre(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2, (y1 + y2) / 2)


class FaceAnalyser:
    """Lazy, thread-safe wrapper around InsightFace.

    Loading the models takes a few seconds, so it happens on first use rather
    than at import -- the app must start (and the rest of it must work) whether
    or not the models are where we expect them.
    """

    def __init__(self, root: str, det_size: int = 640):
        self.root = root
        self.det_size = det_size
        self._app = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return (Path(self.root) / "models" / "buffalo_l").is_dir()

    def _ensure(self):
        if self._app is not None:
            return self._app
        with self._lock:
            if self._app is not None:
                return self._app
            if not self.available():
                raise FaceRejected(
                    "The face detection models are missing. Expected buffalo_l under "
                    f"{self.root}/models/ -- set INSIGHTFACE_ROOT to where ComfyUI keeps them."
                )
            from insightface.app import FaceAnalysis

            app = FaceAnalysis(name="buffalo_l", root=self.root,
                               providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(self.det_size, self.det_size))
            self._app = app
            log.info("insightface ready (cpu, buffalo_l from %s)", self.root)
            return app

    def detect(self, img: Image.Image) -> list[DetectedFace]:
        app = self._ensure()
        # InsightFace expects BGR, which is what it was trained against.
        arr = np.asarray(img.convert("RGB"))[:, :, ::-1]
        faces = app.get(arr)
        return sorted(
            (DetectedFace(tuple(float(v) for v in f.bbox), float(f.det_score)) for f in faces),
            key=lambda f: f.size, reverse=True,
        )


def _open(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise FaceRejected("That file isn't an image we can read (try PNG, JPEG or WebP).")
    # Orientation is applied now: a sideways photo detects badly, and the crop
    # has to match what the person saw when they picked the file.
    return ImageOps.exif_transpose(img).convert("RGB")


def normalise_reference(data: bytes, settings: Settings, analyser: FaceAnalyser,
                        label: str = "that image") -> tuple[bytes, dict]:
    """Validate one reference face and return it as a square, face-centred PNG.

    Every reference comes out at exactly the same size, which is what stops
    ImageBatch from rescaling and cropping the second one onwards.
    """
    if len(data) > settings.upload_max_mb * 1024 * 1024:
        raise FaceRejected(f"{label} is larger than {settings.upload_max_mb:.0f} MB.")

    img = _open(data)
    faces = analyser.detect(img)

    if not faces:
        raise FaceRejected(
            f"No face found in {label}. Use a photo where the face is clearly "
            "visible and not turned too far away."
        )

    # The largest face is the subject; a bystander in the background should not
    # cost someone their upload. Which one was used is reported back.
    face = faces[0]
    if face.size < settings.reference_min_face_px:
        raise FaceRejected(
            f"The face in {label} is only {round(face.size)}px across, which is too "
            f"small to copy a likeness from (needs {settings.reference_min_face_px}px). "
            "Try a closer crop or a higher-resolution photo."
        )

    size = settings.reference_size
    cx, cy = face.centre
    w, h = img.size

    # A square crop around the face, with context: FaceID wants some head and
    # shoulders, not a tight box. Clamped to the image, and kept square so the
    # resize never distorts the face -- an off-centre crop is far better than a
    # stretched one.
    side = min(face.size * settings.reference_margin, float(w), float(h))
    x0 = min(max(cx - side / 2, 0.0), w - side)
    y0 = min(max(cy - side / 2, 0.0), h - side)
    crop = img.crop((round(x0), round(y0), round(x0 + side), round(y0 + side)))
    crop = crop.resize((size, size), Image.LANCZOS)

    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return buf.getvalue(), {
        "width": size,
        "height": size,
        "faces_found": len(faces),
        "face_score": round(face.score, 3),
        "face_px": round(face.size),
    }


_analyser: FaceAnalyser | None = None


def get_analyser(settings: Settings | None = None) -> FaceAnalyser:
    global _analyser
    if _analyser is None:
        from .config import get_settings

        s = settings or get_settings()
        _analyser = FaceAnalyser(s.insightface_root, s.face_det_size)
    return _analyser
