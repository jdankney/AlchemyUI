"""Admin API. Every route is behind require_admin, which checks the forwarded
email against ADMIN_EMAILS on each request (Llamacracy's rule).

Metadata only, by design: who ran what, for how long, at what cost. Nothing
here returns another user's prompts, parameters or images -- the person with
the GPU should not be able to read their friends' prompts or see their face
edits, and likeness photos are what make that line matter.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .db import Database, get_db, now
from .identity import Principal, require_admin
from .power import gpu_stats
from .queue import QueueManager, get_queue

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


async def _names(db: Database) -> dict[int, str]:
    rows = await db.fetch_all("SELECT id, email, display_name FROM users")
    return {r["id"]: r["display_name"] or r["email"] for r in rows}


@router.get("/live")
async def live(qm: QueueManager = Depends(get_queue), db: Database = Depends(get_db)):
    """Polled by the admin page every couple of seconds, which is also what
    keeps nvidia-smi from being run when nobody is looking."""
    return {
        "gpu": await gpu_stats(),
        "queue": qm.admin_view(await _names(db)),
        "resident_checkpoint": qm._resident_checkpoint,
        "comfy_connected": qm._comfy_connected,
        "ts": now(),
    }


@router.get("/users")
async def users(days: int = 30, db: Database = Depends(get_db)):
    since = now() - days * 86400 if days > 0 else 0.0
    rows = await db.fetch_all(
        "SELECT u.id, u.email, u.display_name, u.is_admin, u.disabled, u.last_active_at, "
        "  COUNT(j.id) AS jobs, COALESCE(SUM(j.occupancy_seconds), 0) AS occ, "
        "  COALESCE(SUM(j.energy_wh), 0) AS wh, COALESCE(SUM(j.cost_usd), 0) AS cost "
        "FROM users u LEFT JOIN jobs j ON j.user_id = u.id "
        "  AND j.energy_wh IS NOT NULL AND j.finished_at >= ? "
        "GROUP BY u.id ORDER BY cost DESC, u.id",
        (since,),
    )
    return {"days": days, "users": [{
        "id": r["id"], "email": r["email"], "display_name": r["display_name"],
        "is_admin": bool(r["is_admin"]), "disabled": bool(r["disabled"]),
        "last_active_at": r["last_active_at"], "jobs": r["jobs"],
        # exact: rounding is the page's job, and fixed decimals zero out
        # small numbers rather than merely shortening them
        "gpu_minutes": r["occ"] / 60, "kwh": r["wh"] / 1000, "cost_usd": r["cost"],
    } for r in rows]}


@router.get("/failures")
async def failures(limit: int = 20, db: Database = Depends(get_db)):
    """The friendly message only -- error_detail can carry node inputs, and
    node inputs are prompts."""
    rows = await db.fetch_all(
        "SELECT j.id, j.workflow_id, j.error_text, j.finished_at, u.display_name, u.email "
        "FROM jobs j LEFT JOIN users u ON u.id = j.user_id "
        "WHERE j.status = 'failed' ORDER BY j.finished_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    )
    return {"failures": [{
        "id": r["id"], "workflow_id": r["workflow_id"], "error_text": r["error_text"],
        "finished_at": r["finished_at"],
        "owner": r["display_name"] or r["email"] or "unclaimed",
    } for r in rows]}


class DisabledFlag(BaseModel):
    disabled: bool


@router.post("/users/{user_id}/disabled")
async def set_disabled(user_id: int, body: DisabledFlag,
                       principal: Principal = Depends(require_admin),
                       db: Database = Depends(get_db),
                       qm: QueueManager = Depends(get_queue)):
    if user_id == principal.user_id and body.disabled:
        raise HTTPException(400, "you cannot disable your own account")
    if await db.fetch_one("SELECT id FROM users WHERE id = ?", (user_id,)) is None:
        raise HTTPException(404, "no such user")
    await db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(body.disabled), user_id))
    cancelled = await qm.cancel_user(user_id) if body.disabled else 0
    return {"id": user_id, "disabled": body.disabled, "cancelled": cancelled}
