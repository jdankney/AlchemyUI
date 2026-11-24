"""Phase 9: SearXNG discovery and the SSRF-safe reference fetcher.

All network is a mock transport. The checks that matter are the refusal
ones: private addresses, redirects into private addresses, non-images and
oversized downloads must never reach ComfyUI.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

import alchemyui.search as search


def settings(**kw):
    base = dict(search_url="http://searx.local", search_timeout_s=2.0,
                search_results=8, search_max_mb=1.0, search_max_redirects=2,
                search_user_agent="AlchemyUI-test", upload_max_edge=1536)
    base.update(kw)
    return SimpleNamespace(**base)


def png(size=(96, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 90, 180)).save(buf, format="PNG")
    return buf.getvalue()


async def test_image_search_parses_and_resolves_thumbnails():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"results": [
            {"title": "Nike Pro", "source": "shop", "img_src": "/img/a.jpg",
             "thumbnail_src": "/thumb/a.jpg", "width": 900, "height": 1200},
            {"title": "duplicate", "img_src": "/img/a.jpg"},
            {"title": "other", "img_src": "https://cdn.example/b.jpg",
             "thumbnail_src": "https://cdn.example/b-thumb.jpg"},
        ]})

    got = await search.image_search("Nike Pro sweatpants", settings(),
                                    transport=httpx.MockTransport(handler))
    assert "categories=images" in seen["url"]
    assert "format=json" in seen["url"]
    assert [r["url"] for r in got] == [
        "http://searx.local/img/a.jpg", "https://cdn.example/b.jpg"]
    assert got[0]["thumb_url"] == "http://searx.local/thumb/a.jpg"
    assert got[0]["title"] == "Nike Pro"


async def test_an_unconfigured_search_says_so():
    with pytest.raises(search.SearchError, match="not configured"):
        await search.image_search("x", settings(search_url=""))


async def test_fetch_reference_rejects_private_hosts_before_any_request():
    called = False

    def handler(req):
        nonlocal called
        called = True
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    with pytest.raises(search.SearchError, match="private"):
        await search.fetch_reference("http://127.0.0.1/a.png", settings(),
                                     transport=httpx.MockTransport(handler))
    assert called is False


async def test_fetch_reference_rechecks_a_redirect(monkeypatch):
    called = []

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search, "_resolve_host", public)

    def handler(req):
        called.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/evil.png"})

    with pytest.raises(search.SearchError, match="private"):
        await search.fetch_reference("https://example.com/a.png", settings(),
                                     transport=httpx.MockTransport(handler))
    assert called == ["https://example.com/a.png"]


async def test_fetch_reference_requires_an_image(monkeypatch):
    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search, "_resolve_host", public)
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=b"not an image", headers={"content-type": "text/plain"}))
    with pytest.raises(search.SearchError, match="not an image"):
        await search.fetch_reference("https://example.com/a", settings(), transport=transport)


async def test_fetch_reference_caps_and_reencodes(monkeypatch):
    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search, "_resolve_host", public)
    data = png((300, 200))
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=data, headers={"content-type": "image/png"}))
    png_out, width, height = await search.fetch_reference(
        "https://example.com/a.png", settings(), transport=transport)
    assert png_out.startswith(b"\x89PNG")
    assert (width, height) == (296, 200)   # snapped to the VAE stride

    small = settings(search_max_mb=0.0001)
    with pytest.raises(search.SearchError, match="larger"):
        await search.fetch_reference("https://example.com/a.png", small, transport=transport)


async def test_fetch_reference_rejects_a_too_small_image(monkeypatch):
    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(search, "_resolve_host", public)
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, content=png((32, 32)), headers={"content-type": "image/png"}))
    with pytest.raises(search.SearchError, match="too small"):
        await search.fetch_reference("https://example.com/a.png", settings(), transport=transport)
