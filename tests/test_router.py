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
