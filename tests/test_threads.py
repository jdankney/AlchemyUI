"""The turn engine: planning, handles in the user's own scope, needs_input,
select, the job hook, cancel, retry and restart. FakeRouter stands in for
the LLM; the queue and ComfyUI's script are the real queue over FakeComfy."""

from __future__ import annotations

import asyncio

import pytest

from alchemyui.planner.router import RouteResult
from alchemyui.planner.schema import Plan
from alchemyui.threads import TurnError


def P(**kw):
    base = dict(action="generate", source="", faces="", prompt="a lighthouse", negative="",
                aspect="1:1", count=1, strength="medium", quality="fast", direction="",
                question="", options=())
    base.update(kw)
    return Plan(**base)


async def settle(h, turn_id, states=("done", "failed", "needs_input", "cancelled"), timeout=5):
    t = None
    for _ in range(int(timeout / 0.02)):
        t = await h.engine.turn_public(await h.row(turn_id))
        if t["state"] in states:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"turn stuck in {t and t['state']}")


async def test_text_turn_plans_queues_and_finishes(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=2), "primary", 900, 120, 40)]
    t = await h.engine.create_turn(h.alice, "spooky lighthouse", [])
    assert t["state"] == "planning" and t["seq"] == 1
    done = await settle(h, t["id"])
    assert done["state"] == "done" and len(done["jobs"]) == 2
    assert [i["handle"] for i in done["images"]] == ["t1.1", "t1.2"]
    assert done["chip"] == "Generating 2 · Realistic Vision 6.0 · 1:1"
    assert done["plan_source"] == "primary" and done["router_ms"] == 900
    assert done["model"] == "Realistic Vision 6.0"
    row = await h.row(t["id"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (120, 40)
    threads = await h.engine.list_threads(h.alice)
    assert threads[0]["title"] == "spooky lighthouse"


async def test_the_chip_says_when_a_backup_planned_it(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "fallback"), RouteResult(P(), "default")]
    a = await settle(h, (await h.engine.create_turn(h.alice, "x", []))["id"])
    b = await settle(h, (await h.engine.create_turn(h.alice, "y", []))["id"])
    assert a["chip"].endswith("backup planner") and b["chip"].endswith("planner offline")


async def test_follow_up_sees_earlier_outputs_as_handles_but_never_their_pixels(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary"),
                        RouteResult(P(action="transform", source="t1.1", strength="subtle"),
                                    "primary")]
    t1 = await h.engine.create_turn(h.alice, "a lighthouse", [])
    await settle(h, t1["id"])
    t2 = await h.engine.create_turn(h.alice, "make it darker", [], thread_id=t1["thread_id"])
    done = await settle(h, t2["id"])
    req = h.router.requests[1]
    assert "t1.1" in req.sources and req.attachments == []
    assert any("Current image: t1.1" in line for line in req.context)
    assert any('prompt: "a lighthouse"' in line for line in req.context)
    assert done["state"] == "done" and done["jobs"][0]["params"]["denoise"] == 0.35
    assert done["jobs"][0]["workflow_id"] == "img2img-sdxl"


async def test_another_users_thread_and_images_are_never_in_scope(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary"), RouteResult(P(), "primary")]
    a = await h.engine.create_turn(h.alice, "alice's", [])
    await settle(h, a["id"])
    with pytest.raises(TurnError, match="no such thread"):
        await h.engine.create_turn(h.bob, "steal", [], thread_id=a["thread_id"])
    with pytest.raises(TurnError, match="no such turn"):
        await h.engine.cancel(h.bob, a["id"])
    b = await h.engine.create_turn(h.bob, "bob's", [])
    await settle(h, b["id"])
    assert h.router.requests[1].sources == [] and h.router.requests[1].context == []


async def test_attachment_of_another_user_is_refused(engine_harness):
    h = engine_harness
    up = await h.upload(h.alice)
    with pytest.raises(TurnError, match="no such upload"):
        await h.engine.create_turn(h.bob, "mine now", [up])


async def test_attachments_are_sent_as_labelled_thumbnails(engine_harness):
    h = engine_harness
    a1, a2 = await h.upload(h.alice), await h.upload(h.alice, (2000, 1000))
    h.router.results = [RouteResult(P(action="transform", source="a2"), "primary")]
    t = await h.engine.create_turn(h.alice, "make the second one snowy", [a1, a2])
    done = await settle(h, t["id"])
    req = h.router.requests[0]
    assert [a.handle for a in req.attachments] == ["a1", "a2"]
    assert all(a.data_url.startswith("data:image/jpeg") for a in req.attachments)
    assert [a["handle"] for a in done["attachments"]] == ["a1", "a2"]
    assert done["state"] == "done"


async def test_ask_parks_without_a_queue_place_and_an_answer_replans(engine_harness):
    h = engine_harness
    h.comfy.mode = "hang"            # anything queued would stay visible in the snapshot
    h.router.results = [RouteResult(P(action="ask", question="Which one?", options=("1", "2")),
                                    "primary"),
                        RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "hmm", [])
    parked = await settle(h, t["id"])
    assert parked["state"] == "needs_input" and parked["jobs"] == []
    assert parked["question"] == {"question": "Which one?", "options": ["1", "2"]}
    assert h.qm.snapshot()["depth"] == 0
    await h.engine.answer(h.alice, t["id"], None, 1)
    await settle(h, t["id"], ("queued",))
    assert h.router.requests[1].answers == [{"question": "Which one?", "answer": "2"}]
    with pytest.raises(TurnError, match="isn't waiting"):
        await h.engine.answer(h.alice, t["id"], "again", None)


async def test_a_typed_answer_is_replayed_to_the_router(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(action="ask", question="Portrait or landscape?"), "primary"),
                        RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "a castle", [])
    await settle(h, t["id"])
    await h.engine.answer(h.alice, t["id"], "landscape please", None)
    done = await settle(h, t["id"], ("done",))
    assert h.router.requests[1].answers[0]["answer"] == "landscape please"
    assert done["answers"] == [{"question": "Portrait or landscape?", "answer": "landscape please"}]


async def test_default_planner_options_run_without_the_llm(engine_harness):
    h = engine_harness
    up = await h.upload(h.alice, (1024, 1024))
    plan = P(action="enhance", source="a1").as_dict()
    h.router.results = [RouteResult(None, "default", question={
        "question": "Pick one:", "options": [{"label": "Enhance it", "plan": plan}]})]
    t = await h.engine.create_turn(h.alice, "", [up])
    parked = await settle(h, t["id"])
    assert parked["question"]["options"] == ["Enhance it"]
    await h.engine.answer(h.alice, t["id"], None, 0)
    done = await settle(h, t["id"], ("done", "failed"))
    assert len(h.router.requests) == 1
    assert done["jobs"][0]["workflow_id"] == "enhance-sdxl"
    assert done["chip"].endswith("planner offline")


async def test_likeness_without_saved_faces_asks(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(action="likeness", faces="me"), "primary")]
    t = await h.engine.create_turn(h.alice, "me as an astronaut", [])
    parked = await settle(h, t["id"])
    assert parked["state"] == "needs_input" and "likeness" in parked["question"]["question"]
    assert h.router.requests[0].faces == ["me"]


async def test_likeness_uses_the_saved_likeness(engine_harness):
    h = engine_harness
    await h.likeness(h.alice, n=2)
    await h.likeness(h.bob, n=1)
    h.router.results = [RouteResult(P(action="likeness", faces="me"), "primary")]
    t = await h.engine.create_turn(h.alice, "me as an astronaut", [])
    done = await settle(h, t["id"])
    assert "me" in h.router.requests[0].faces
    refs = done["jobs"][0]["params"]["references"]
    assert len(refs) == 2 and all(f"ref-{h.alice}-" in r for r in refs)


async def test_select_moves_focus_and_queues_nothing(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=2), "primary"),
                        RouteResult(P(action="select", source="t1.2"), "primary"),
                        RouteResult(P(action="transform", source="t1.2"), "primary")]
    t1 = await h.engine.create_turn(h.alice, "two foxes", [])
    await settle(h, t1["id"])
    t2 = await h.engine.create_turn(h.alice, "use the second one", [], thread_id=t1["thread_id"])
    sel = await settle(h, t2["id"])
    assert sel["state"] == "done" and sel["jobs"] == [] and "image 2" in sel["reply"]
    t3 = await h.engine.create_turn(h.alice, "now add a dog", [], thread_id=t1["thread_id"])
    await settle(h, t3["id"])
    assert any("Current image: t1.2 (picked" in line for line in h.router.requests[2].context)


async def test_several_new_images_are_ambiguous_to_the_router(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(count=3), "primary"), RouteResult(P(), "primary")]
    t1 = await h.engine.create_turn(h.alice, "three foxes", [])
    await settle(h, t1["id"])
    t2 = await h.engine.create_turn(h.alice, "make it darker", [], thread_id=t1["thread_id"])
    await settle(h, t2["id"])
    assert any("none picked yet" in line for line in h.router.requests[1].context)


async def test_cancel_a_queued_turn_cancels_its_jobs(engine_harness):
    h = engine_harness
    h.comfy.steps, h.comfy.delay = 200, 0.02   # slow, but it answers an interrupt
    h.router.results = [RouteResult(P(count=2), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    await settle(h, t["id"], ("queued",))
    await asyncio.sleep(0.1)
    await h.engine.cancel(h.alice, t["id"])
    for _ in range(100):
        done = await h.engine.turn_public(await h.row(t["id"]))
        if done["finished_at"]:
            break
        await asyncio.sleep(0.02)
    assert done["state"] == "cancelled"
    assert h.comfy.interrupts
    assert all(j["status"] == "cancelled" for j in done["jobs"])
    assert done["finished_at"] is not None
    with pytest.raises(TurnError, match="already finished"):
        await h.engine.cancel(h.alice, t["id"])


async def test_cancel_while_planning(engine_harness):
    h = engine_harness
    h.router.hold = True
    t = await h.engine.create_turn(h.alice, "x", [])
    await asyncio.sleep(0.05)
    done = await h.engine.cancel(h.alice, t["id"])
    assert done["state"] == "cancelled" and done["jobs"] == []
    h.router.hold = False


async def test_failed_jobs_fail_the_turn_with_the_reason(engine_harness):
    h = engine_harness
    h.comfy.mode = "error"
    h.router.results = [RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    done = await settle(h, t["id"])
    assert done["state"] == "failed" and "KSampler" in done["reply"]


async def test_restart_replans_turns_left_planning(engine_harness):
    h = engine_harness
    h.router.hold = True                        # planning never returns
    t = await h.engine.create_turn(h.alice, "x", [])
    await asyncio.sleep(0.05)
    await h.engine.stop()
    h.router.hold = False
    h.router.results = [RouteResult(P(), "primary")]
    await h.restart_engine()
    done = await settle(h, t["id"])
    assert done["state"] == "done"


async def test_retry_runs_the_same_plan_with_new_seeds(engine_harness):
    h = engine_harness
    h.router.results = [RouteResult(P(), "primary")]
    t = await h.engine.create_turn(h.alice, "x", [])
    await settle(h, t["id"])
    await h.engine.retry(h.alice, t["id"])
    await settle(h, t["id"], ("queued", "done"))
    for _ in range(100):
        again = await h.engine.turn_public(await h.row(t["id"]))
        if again["state"] == "done" and len(again["images"]) == 2:
            break
        await asyncio.sleep(0.02)
    assert len(again["jobs"]) == 2 and len(h.router.requests) == 1
    assert len({j["params"]["seed"] for j in again["jobs"]}) == 2
    assert [i["handle"] for i in again["images"]] == ["t1.1", "t1.2"]


async def test_empty_and_oversized_messages_are_refused(engine_harness):
    h = engine_harness
    with pytest.raises(TurnError, match="type a message"):
        await h.engine.create_turn(h.alice, "  ", [])
    ups = [await h.upload(h.alice) for _ in range(5)]
    with pytest.raises(TurnError, match="at most 4"):
        await h.engine.create_turn(h.alice, "x", ups)
