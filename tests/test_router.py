"""The router against replayed LLM responses: the client's failure
classification, what the model is told, and the primary -> repair ->
fallback -> default chain. No model is needed; router-eval covers real ones."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from alchemyui.planner.client import LLMClient, LLMError
from alchemyui.planner.prompt import Attachment, build_messages, system_prompt, thumbnail
from tests.test_planner_schema import V


def _reply(content, status=200):
    body = {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}
    return httpx.Response(status, json=body)


# --- client --------------------------------------------------------------------
async def test_client_sends_schema_thinking_off_and_key():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return _reply('{"ok": 1}')

    c = LLMClient("primary", "http://llm/v1/", "m", api_key="k",
                  transport=httpx.MockTransport(handler))
    r = await c.complete([{"role": "user", "content": "hi"}], {"type": "object"})
    assert r.content == '{"ok": 1}' and r.prompt_tokens == 50 and r.completion_tokens == 20
    b = seen["body"]
    assert seen["auth"] == "Bearer k" and b["model"] == "m"
    assert seen["url"] == "http://llm/v1/chat/completions"
    assert b["response_format"]["type"] == "json_schema"
    assert b["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert b["chat_template_kwargs"] == {"enable_thinking": False}
    assert b["thinking_budget_tokens"] == 0 and b["temperature"] == 0
    assert c.status()["ok"] is True


@pytest.mark.parametrize("response, kind", [
    (httpx.Response(502, json={"detail": 'upstream 500: {"error":{"message":"image input '
                                         'is not supported - hint: provide the mmproj"}}'}),
     "no_vision"),
    (httpx.Response(502, text="bad gateway"), "unreachable"),
    (httpx.Response(401, json={"detail": "bad key"}), "rejected"),
    (httpx.Response(200, json={"choices": []}), "bad_output"),
])
async def test_client_classifies_failures(response, kind):
    c = LLMClient("p", "http://llm/v1", "m", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(LLMError) as e:
        await c.complete([], {})
    assert e.value.kind == kind
    # a model that cannot see is still a healthy backend
    assert c.status()["ok"] is (kind == "no_vision")


async def test_a_plan_cut_off_by_the_token_cap_is_bad_output():
    body = {"choices": [{"message": {"content": '{"action": "gen'}, "finish_reason": "length"}]}
    c = LLMClient("p", "http://llm/v1", "m",
                  transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(LLMError, match="ran out of tokens") as e:
        await c.complete([], {})
    assert e.value.kind == "bad_output" and c.status()["ok"] is True


async def test_client_connect_failure_is_unreachable():
    def boom(req):
        raise httpx.ConnectError("refused")

    c = LLMClient("p", "http://llm/v1", "m", transport=httpx.MockTransport(boom))
    with pytest.raises(LLMError) as e:
        await c.complete([], {})
    assert e.value.kind == "unreachable" and "unreachable" in c.status()["error"]


async def test_client_timeout_is_its_own_kind():
    def slow(req):
        raise httpx.ReadTimeout("slow")

    c = LLMClient("p", "http://llm/v1", "m", transport=httpx.MockTransport(slow))
    with pytest.raises(LLMError) as e:
        await c.complete([], {})
    assert e.value.kind == "timeout"


# --- prompt --------------------------------------------------------------------
def test_thumbnail_is_a_small_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (2000, 1000), "red").save(buf, "PNG")
    url = thumbnail(buf.getvalue(), 768)
    assert url.startswith("data:image/jpeg;base64,")
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert im.size == (768, 384)


def test_messages_label_each_attachment_and_carry_context():
    msgs = build_messages(V, "put me in it", [Attachment("a1", "data:x"), Attachment("a2", "data:y")],
                          ['Turn 1: "a bed" -> generate. Images: t1.1'], True,
                          [{"question": "Which one?", "answer": "the first"}])
    assert msgs[0]["role"] == "system" and "transform" in msgs[0]["content"]
    parts = msgs[1]["content"]
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert "a1:" in texts and "a2:" in texts
    assert [p["image_url"]["url"] for p in parts if p["type"] == "image_url"] == ["data:x", "data:y"]
    blob = " ".join(texts)
    assert "t1.1" in blob and "saved likeness: yes" in blob and "the first" in blob
    assert "put me in it" in blob


def test_text_only_messages_describe_but_do_not_send_images():
    msgs = build_messages(V, "x", [Attachment("a1", "data:x")], [], False, [], images=False)
    assert all(p["type"] == "text" for p in msgs[1]["content"])
    assert "a1" in msgs[1]["content"][0]["text"]


def test_system_prompt_lists_only_the_vocabulary():
    sp = system_prompt(V)
    for a in V.actions:
        assert f"- {a}:" in sp
    assert "edit_region" not in sp and "with_reference" not in sp


# --- router --------------------------------------------------------------------
from alchemyui.planner.client import LLMReply  # noqa: E402
from alchemyui.planner.router import Router, RouteRequest  # noqa: E402

GOOD = json.dumps(dict(action="generate", source="", faces="", prompt="lighthouse, night",
                       negative="", aspect="1:1", count=1, strength="medium", quality="fast",
                       direction="", question="", options=[]))
TRANSFORM_A1 = json.dumps({**json.loads(GOOD), "action": "transform", "source": "a1"})


class Scripted:
    """An LLMClient stand-in that replays recorded replies, or raises."""

    def __init__(self, name, *script):
        self.name, self.script, self.calls = name, list(script), []

    async def complete(self, messages, schema, max_tokens=400):
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return LLMReply(step, 100, 30, 5)

    def status(self):
        return {"name": self.name}


def req(text="a spooky lighthouse", attachments=(), sources=(), faces=()):
    return RouteRequest(text, list(attachments), [], False, [], list(sources), list(faces))


def _sent_images(messages):
    return [p for p in messages[1]["content"] if p["type"] == "image_url"]


async def test_primary_plan_is_used():
    p = Scripted("primary", GOOD)
    r = await Router(V, p, Scripted("fallback")).route(req())
    assert r.source == "primary" and r.plan.action == "generate" and r.errors == []
    assert (r.prompt_tokens, r.completion_tokens, r.ms) == (100, 30, 5)


async def test_invalid_plan_gets_one_repair_with_the_error():
    bad = GOOD.replace('"generate"', '"transform"')
    p = Scripted("primary", bad, GOOD)
    r = await Router(V, p, None).route(req())
    assert r.source == "primary" and r.plan.action == "generate"
    assert "needs a source image" in p.calls[1][-1]["content"]
    assert p.calls[1][-2] == {"role": "assistant", "content": bad}
    assert r.prompt_tokens == 200          # both attempts are counted


async def test_unreachable_primary_falls_back():
    p = Scripted("primary", LLMError("unreachable", "down"))
    f = Scripted("fallback", GOOD)
    r = await Router(V, p, f).route(req())
    assert r.source == "fallback" and "down" in r.errors[0]


async def test_no_vision_primary_hands_image_turns_to_the_fallback():
    p = Scripted("primary", LLMError("no_vision", "cannot see"))
    f = Scripted("fallback", TRANSFORM_A1)
    r = await Router(V, p, f).route(req(attachments=[Attachment("a1", "data:x")], sources=["a1"]))
    assert r.source == "fallback" and r.plan.action == "transform"
    assert _sent_images(f.calls[0])


async def test_no_vision_and_no_fallback_plans_blind_on_the_primary():
    """Better a model that knows an image is attached than no model at all."""
    p = Scripted("primary", LLMError("no_vision", "cannot see"), TRANSFORM_A1)
    f = Scripted("fallback", LLMError("unreachable", "down"))
    r = await Router(V, p, f).route(req("make it an oil painting",
                                        attachments=[Attachment("a1", "data:x")], sources=["a1"]))
    assert r.source == "primary" and r.plan.action == "transform"
    assert _sent_images(p.calls[1]) == [] and "cannot see them" in p.calls[1][1]["content"][0]["text"]


async def test_both_down_text_only_generates_the_users_words():
    r = await Router(V, Scripted("p", LLMError("unreachable", "x")),
                     Scripted("f", LLMError("timeout", "y"))).route(req("Snowy Forest during Snowstorm"))
    assert r.source == "default" and r.plan.action == "generate"
    assert r.plan.prompt == "Snowy Forest during Snowstorm"
    assert len(r.errors) == 2


async def test_both_down_with_attachments_asks_with_ready_made_options():
    r = await Router(V, None, None).route(
        req("x", attachments=[Attachment("a1", "data:x")], sources=["a1"]))
    assert r.source == "default" and r.plan is None
    labels = [o["label"] for o in r.question["options"]]
    assert labels == ["Restyle it", "Enhance it", "Make it wider"]
    assert all(o["plan"]["source"] == "a1" for o in r.question["options"])


async def test_twice_invalid_moves_on_to_the_fallback():
    p = Scripted("primary", "nope", "still nope")
    f = Scripted("fallback", GOOD)
    r = await Router(V, p, f).route(req())
    assert r.source == "fallback" and len(p.calls) == 2
    assert all("invalid plan" in e for e in r.errors)
