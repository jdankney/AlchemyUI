"""Model profiles: tuned defaults for a checkpoint (Phase 7.5).

The registry file says how each model runs out of the box. A profile, adopted
from the Photoreal Lab, overrides some of that -- steps, cfg, sampler and
scheduler, a "look" appended to every prompt, and a negative prompt -- for
everyone, until it is reset. Profiles live in the database and are laid over
the registry at startup and whenever one changes, so the committed file stays
the baseline and a tuned value never has to be hand-edited into it.
"""

from __future__ import annotations

import json

from .db import Database, now
from .registry import CheckpointInfo, Registry

PROFILE_KEYS = ("steps", "cfg", "sampler", "scheduler", "look", "negative")
TEXT_MAX = 400
STEPS = (1, 80)
CFG = (1.0, 15.0)


class ProfileInvalid(ValueError):
    """The settings cannot be a profile. The message is shown to the admin."""


def _choices(reg: Registry) -> dict[str, tuple[str, ...]]:
    """Samplers and schedulers the generation workflow accepts: a profile
    applies to it above all, so nothing it would refuse can be adopted."""
    wf = next((w for w in reg.all() if w.kind == "txt2img"), None)
    return dict(wf.choices) if wf else {}


def validate(settings: dict, reg: Registry) -> dict:
    unknown = set(settings) - set(PROFILE_KEYS)
    if unknown:
        raise ProfileInvalid(f"not a profile setting: {', '.join(sorted(unknown))}")
    choices = _choices(reg)
    out: dict = {}
    for key, value in settings.items():
        if key in ("steps", "cfg"):
            try:
                n = float(value)
            except (TypeError, ValueError):
                raise ProfileInvalid(f"{key} must be a number") from None
            lo, hi = STEPS if key == "steps" else CFG
            n = max(lo, min(hi, n))
            out[key] = int(round(n)) if key == "steps" else round(n, 2)
        elif key in ("sampler", "scheduler"):
            allowed = choices.get(key, ())
            if value not in allowed:
                raise ProfileInvalid(f"{value!r} is not a {key} this app can run")
            out[key] = value
        else:
            out[key] = " ".join(str(value).split())[:TEXT_MAX]
    return out


def compose(prompt: str, negative_extra: str, ck: CheckpointInfo | None,
            house_negative: str) -> tuple[str, str]:
    """The prompt and negative a job actually runs with.

    The model's look goes after the words the planner wrote; its own negative,
    when it has one, replaces the house negative (an empty one means none).
    The planner's negative, which is about this picture, always comes first.
    """
    defaults = ck.defaults if ck else {}
    look = str(defaults.get("look") or "").strip()
    p = prompt.strip()
    if look and p:
        p = f"{p}, {look}"
    base = str(defaults["negative"]) if "negative" in defaults else house_negative
    negative = ", ".join(x for x in (negative_extra.strip(), base.strip()) if x)
    return p, negative


async def profile_rows(db: Database) -> dict[str, dict]:
    rows = await db.fetch_all("SELECT * FROM model_profiles")
    return {r["checkpoint"]: {"settings": json.loads(r["settings_json"]),
                              "cover_image_id": r["cover_image_id"],
                              "updated_at": r["updated_at"]} for r in rows}


async def load_profiles(db: Database, reg: Registry) -> None:
    """Lay every stored profile over the registry. A profile for a checkpoint
    that has since left the registry is ignored, not fatal."""
    for name, p in (await profile_rows(db)).items():
        if reg.base(name) is not None:
            reg.set_override(name, p["settings"])


async def save_profile(db: Database, reg: Registry, name: str, settings: dict,
                       user_id: int | None, cover_image_id: str | None = None) -> dict:
    if name not in {c.name for c in reg.switchable()}:
        raise ProfileInvalid("only a model in the model menu can have a profile")
    clean = validate(settings, reg)
    await db.execute(
        "INSERT INTO model_profiles (checkpoint, settings_json, cover_image_id, updated_by, "
        "  updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(checkpoint) DO UPDATE SET settings_json = excluded.settings_json, "
        "  cover_image_id = COALESCE(excluded.cover_image_id, model_profiles.cover_image_id), "
        "  updated_by = excluded.updated_by, updated_at = excluded.updated_at",
        (name, json.dumps(clean), cover_image_id, user_id, now()))
    reg.set_override(name, clean)
    return clean


async def reset_profile(db: Database, reg: Registry, name: str) -> None:
    await db.execute("DELETE FROM model_profiles WHERE checkpoint = ?", (name,))
    if reg.base(name) is not None:
        reg.set_override(name, None)
