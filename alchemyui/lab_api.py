"""The Photoreal Lab's API. Admin only, like the rest of /api/admin: the Lab
changes how every person's images are made, so only an admin may run it.

What it shows is the Lab's own renders of its fixed test prompts -- never
anyone's messages or pictures -- so the admin rule about other people's
content is untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .db import Database, get_db
from .identity import Principal, require_admin
from .lab import LabEngine, LabInvalid, base_settings, get_lab
from .profiles import ProfileInvalid, profile_rows, reset_profile

router = APIRouter(prefix="/api/lab", dependencies=[Depends(require_admin)])


class Axis(BaseModel):
    param: str
    values: list = Field(default_factory=list)


class RunIn(BaseModel):
    title: str = ""
    checkpoints: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    custom: str = ""
    seeds: int = 1
    axes: list[Axis] = Field(default_factory=list)


class StarIn(BaseModel):
    stars: int = Field(ge=0, le=3)


def _spec(body: RunIn) -> dict:
    return {**body.model_dump(), "axes": [a.model_dump() for a in body.axes]}


async def _call(coro):
    try:
        return await coro
    except (LabInvalid, ProfileInvalid) as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "no such run or image")


@router.get("/presets")
async def presets(lab: LabEngine = Depends(get_lab)):
    return lab.presets


@router.post("/estimate")
async def estimate(body: RunIn, lab: LabEngine = Depends(get_lab)):
    try:
        return lab.estimate(_spec(body))
    except LabInvalid as e:
        raise HTTPException(400, str(e))


@router.get("/runs")
async def runs(lab: LabEngine = Depends(get_lab)):
    return {"runs": await lab.list_runs()}


@router.post("/runs")
async def create_run(body: RunIn, principal: Principal = Depends(require_admin),
                     lab: LabEngine = Depends(get_lab)):
    return await _call(lab.create_run(principal.user_id, _spec(body)))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, lab: LabEngine = Depends(get_lab)):
    return await _call(lab.run_public(run_id))


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, lab: LabEngine = Depends(get_lab)):
    return await _call(lab.cancel_run(run_id))


@router.post("/cells/{cell_id}/star")
async def star(cell_id: str, body: StarIn, lab: LabEngine = Depends(get_lab)):
    return await _call(lab.star(cell_id, body.stars))


@router.post("/cells/{cell_id}/adopt")
async def adopt(cell_id: str, principal: Principal = Depends(require_admin),
                lab: LabEngine = Depends(get_lab)):
    return await _call(lab.adopt(cell_id, principal.user_id))


@router.get("/profiles")
async def profiles(lab: LabEngine = Depends(get_lab), db: Database = Depends(get_db)):
    """Each tunable model: what it runs with now, what the registry file says,
    and the adopted profile's cover. The difference is what to commit to
    workflows/registry.json once a setting has proved itself."""
    rows = await profile_rows(db)
    out = []
    for c in lab.reg.switchable():
        base = lab.reg.base(c.name)
        p = rows.get(c.name)
        out.append({
            "name": c.name, "label": c.label,
            "base": {k: base.defaults[k] for k in base.defaults},
            "settings": p["settings"] if p else {},
            "effective": base_settings(lab.reg, c.name, lab.presets),
            "cover_image_id": p["cover_image_id"] if p else None,
            "updated_at": p["updated_at"] if p else None,
        })
    return {"profiles": out}


@router.delete("/profiles")
async def reset(name: str, lab: LabEngine = Depends(get_lab), db: Database = Depends(get_db)):
    if lab.reg.base(name) is None:
        raise HTTPException(404, "no such model")
    await reset_profile(db, lab.reg, name)
    return {"reset": name}
