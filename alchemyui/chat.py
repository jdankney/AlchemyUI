"""The chat API: threads and turns. A thin layer over the ChatEngine, which
owns every rule; this file only maps its refusals onto HTTP."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .identity import Principal, get_principal
from .threads import ChatEngine, TurnError, get_engine

router = APIRouter(prefix="/api")


class TurnIn(BaseModel):
    thread_id: str | None = None
    text: str = Field(default="", max_length=8000)
    attachments: list[str] = Field(default_factory=list)


class AnswerIn(BaseModel):
    text: str | None = None
    option: int | None = None


async def _call(coro):
    try:
        return await coro
    except TurnError as e:
        raise HTTPException(e.status, str(e))


@router.get("/threads")
async def list_threads(principal: Principal = Depends(get_principal),
                       engine: ChatEngine = Depends(get_engine)):
    return {"threads": await engine.list_threads(principal.user_id)}


@router.get("/threads/{thread_id}")
async def get_thread(thread_id: str, principal: Principal = Depends(get_principal),
                     engine: ChatEngine = Depends(get_engine)):
    return await _call(engine.get_thread(principal.user_id, thread_id))


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, principal: Principal = Depends(get_principal),
                        engine: ChatEngine = Depends(get_engine)):
    """Hidden from the rail. Its images stay in the library."""
    await _call(engine.delete_thread(principal.user_id, thread_id))
    return {"deleted": thread_id}


@router.post("/turns")
async def create_turn(body: TurnIn, principal: Principal = Depends(get_principal),
                      engine: ChatEngine = Depends(get_engine)):
    """Returns at once in `planning`; the rest arrives on /api/events."""
    return await _call(engine.create_turn(principal.user_id, body.text, body.attachments,
                                          body.thread_id))


@router.post("/turns/{turn_id}/answer")
async def answer_turn(turn_id: str, body: AnswerIn,
                      principal: Principal = Depends(get_principal),
                      engine: ChatEngine = Depends(get_engine)):
    return await _call(engine.answer(principal.user_id, turn_id, body.text, body.option))


@router.post("/turns/{turn_id}/cancel")
async def cancel_turn(turn_id: str, principal: Principal = Depends(get_principal),
                      engine: ChatEngine = Depends(get_engine)):
    return await _call(engine.cancel(principal.user_id, turn_id))


@router.post("/turns/{turn_id}/retry")
async def retry_turn(turn_id: str, principal: Principal = Depends(get_principal),
                     engine: ChatEngine = Depends(get_engine)):
    return await _call(engine.retry(principal.user_id, turn_id))
